"""
Novel translation service.

Relay-first chain (see get_translator): Model 1 -> Model 2, both speaking the
OpenAI-compatible chat/completions protocol, with per-novel memory that the
translator both reads and updates.
"""
import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Dict, List, Literal, Optional
from urllib.parse import urlparse

import google.generativeai as genai
from dotenv import load_dotenv

logger = logging.getLogger("novel-reader.translator")

load_dotenv()


from ai_provider import (
    OPENCODE_SESSION_HEADER,
    build_relay_headers,
    call_ai_provider,
    is_opencode_endpoint,
)


class RelayAuthError(RuntimeError):
    """The relay rejected the credential permanently (HTTP 401/403 — key
    disabled, invalid, or forbidden on the router dashboard).

    Raised so callers can STOP work immediately (don't retry, don't walk the
    same-key fallback chain, and don't fail every chapter of a batch) and
    surface a clear 'check your key' message instead."""



@dataclass
class TranslationResult:
    translated_text: str
    model_used: str
    input_tokens: int
    output_tokens: int
    estimated_cost: float
    success: bool
    error: Optional[str] = None


@dataclass
class StreamChunk:
    delta: str = ""
    is_final: bool = False
    result: Optional["MemoryTranslationResult"] = None


class GeminiTranslator:
    """Gemini AI translation service for novels"""
    
    # Model pricing (per 1M tokens) - Free tier limits apply
    PRICING = {
        "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
        "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40},
        "gemini-1.5-flash": {"input": 0.075, "output": 0.30},  # $0.075/1M input, $0.30/1M output
        "gemini-1.5-flash-8b": {"input": 0.0375, "output": 0.15},
        "gemini-1.5-pro": {"input": 1.25, "output": 5.0},
    }
    
    # Free tier: 15 RPM, 1M tokens/day for flash
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "gemini-flash-latest",
        temperature: float = 0.3,
    ):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self.model_name = model
        self.temperature = temperature
        
        if not self.api_key:
            raise ValueError("Gemini API key required")
        
        genai.configure(api_key=self.api_key)
        self.model = genai.GenerativeModel(
            model_name=model,
            generation_config=genai.GenerationConfig(
                temperature=temperature,
                max_output_tokens=8192,
            )
        )
        
        self.pricing = self.PRICING.get(model, {"input": 0.075, "output": 0.30})

    def _generate(self, prompt: str, session_id: Optional[str] = None, **kwargs) -> str:
        """Single model call returning raw text. Overridden by the relay fallback."""
        response = self.model.generate_content(prompt)
        return response.text or ""

    def _generate_stream(self, prompt: str, session_id: Optional[str] = None, **kwargs):
        """Yield text chunks from the model in real time."""
        if hasattr(self, "model") and self.model:
            response = self.model.generate_content(prompt, stream=True)
            for chunk in response:
                if chunk.text:
                    yield chunk.text
        else:
            yield self._generate(prompt, session_id=session_id, **kwargs)

    def translate_short(self, text: str, source_lang: str, target_lang: str = "en", session_id: Optional[str] = None) -> str:
        """Translate a short string (title, synopsis) — no chunking, tolerant."""
        if not text or not text.strip():
            return text or ""
        lang_names = {
            "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "en": "English",
        }
        src = lang_names.get(source_lang, source_lang)
        tgt = lang_names.get(target_lang, target_lang)
        if src == tgt:
            return text.strip()
        prompt = (
            f"You are a professional novel translator. Translate the following text from "
            f"{src} to {tgt}. Output ONLY the translation, nothing else.\n\n{text.strip()}"
        )
        # NOTE: exceptions are NOT swallowed here — FallbackTranslator._run relies
        # on them to retry on the relay when the primary (Gemini) quota is empty.
        result = self._generate(prompt, session_id=session_id).strip()
        # Guard against the model echoing the source back (occurs under load)
        if not result or result == text.strip():
            raise RuntimeError("translate_short returned empty/echoed input")
        return result

    def _build_prompt(
        self,
        text: str,
        source_lang: str,
        target_lang: str = "en",
        quality: Literal["fast", "balanced", "quality"] = "balanced",
        context: Optional[str] = None,
        glossary: Optional[Dict[str, str]] = None,
    ) -> str:
        """Build translation prompt based on quality setting"""
        
        lang_names = {
            "zh": "Chinese", "ja": "Japanese", "ko": "Korean",
            "en": "English", "zh-CN": "Chinese (Simplified)", "zh-TW": "Chinese (Traditional)",
        }
        
        src_name = lang_names.get(source_lang, source_lang)
        tgt_name = lang_names.get(target_lang, target_lang)
        
        # Quality-specific instructions
        quality_instructions = {
            "fast": "Translate quickly. Prioritize speed over nuance. Keep sentences natural but don't over-polish.",
            "balanced": "Translate accurately with natural flow. Preserve character voices, honorifics, and cultural nuances. Adapt idioms naturally.",
            "quality": "Translate with maximum literary quality. Preserve all nuances, tone, character voices, honorifics, cultural references. Polish prose to publication quality. Handle wordplay, puns, and cultural references with explanatory adaptations where needed.",
        }
        
        glossary_text = ""
        if glossary:
            glossary_text = "\n\nGLOSSARY (MUST USE THESE EXACT TRANSLATIONS):\n"
            for k, v in glossary.items():
                glossary_text += f"- {k} → {v}\n"
        
        context_text = ""
        if context:
            context_text = f"\n\nCONTEXT:\n{context}\n"
        
        prompt = f"""You are a professional light novel / web novel translator. Translate from {src_name} to {tgt_name}.

QUALITY MODE: {quality.upper()}
{quality_instructions[quality]}

{glossary_text}
{context_text}

SOURCE TEXT:
{text}

TRANSLATION RULES:
1. Keep character names, place names, and proper nouns consistent
2. Preserve honorifics (-san, -kun, -chan, -sama, -nim, -ssi, xiānsheng, xiǎojiě, etc.) unless they sound unnatural in English
3. Translate cultivation terms, technique names, and fantasy terminology consistently
4. Keep sound effects (SFX) if they add atmosphere, or translate them in brackets
5. PARAGRAPH FORMAT: put ONE BLANK LINE between every paragraph, including
   between separate lines of dialogue. The source often uses a single line
   break per paragraph; your output must use a blank line instead. Never run
   several paragraphs together into one block.
6. DO NOT add explanations, notes, or meta-commentary
7. Output ONLY the translated text

TRANSLATE NOW:"""
        
        return prompt

    def translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str = "en",
        quality: Literal["fast", "balanced", "quality"] = "balanced",
        context: Optional[str] = None,
        glossary: Optional[Dict[str, str]] = None,
        session_id: Optional[str] = None,
    ) -> TranslationResult:
        """Synchronous translation"""
        try:
            prompt = self._build_prompt(text, source_lang, target_lang, quality, context, glossary)
            generated = self._generate(prompt, session_id=session_id)

            if not generated:
                return TranslationResult(
                    translated_text="",
                    model_used=self.model_name,
                    input_tokens=0,
                    output_tokens=0,
                    estimated_cost=0.0,
                    success=False,
                    error="Empty response from model"
                )
            
            # Estimate tokens (rough approximation)
            input_tokens = len(prompt) // 4
            output_tokens = len(generated) // 4
            
            input_cost = (input_tokens / 1_000_000) * self.pricing["input"]
            output_cost = (output_tokens / 1_000_000) * self.pricing["output"]
            total_cost = input_cost + output_cost
            
            return TranslationResult(
                translated_text=generated.strip(),
                model_used=self.model_name,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost=total_cost,
                success=True,
            )
            
        except Exception as e:
            return TranslationResult(
                translated_text="",
                model_used=self.model_name,
                input_tokens=0,
                output_tokens=0,
                estimated_cost=0.0,
                success=False,
                error=str(e)
            )

    async def translate_async(
        self,
        text: str,
        source_lang: str,
        target_lang: str = "en",
        quality: Literal["fast", "balanced", "quality"] = "balanced",
        context: Optional[str] = None,
        glossary: Optional[Dict[str, str]] = None,
        session_id: Optional[str] = None,
    ) -> TranslationResult:
        """Async wrapper for translation"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            self.translate,
            text, source_lang, target_lang, quality, context, glossary, session_id
        )

    def translate_chapter(
        self,
        text: str,
        source_lang: str,
        target_lang: str = "en",
        quality: Literal["fast", "balanced", "quality"] = "balanced",
        previous_context: Optional[str] = None,
        glossary: Optional[Dict[str, str]] = None,
        session_id: Optional[str] = None,
    ) -> TranslationResult:
        """Translate a full chapter (handles long text by chunking)"""
        # For now, translate in one go (Gemini 1.5 Flash has 1M token context)
        # If text is very long, we could chunk it
        max_chars = 300000  # ~75k tokens, well within limits
        
        if len(text) <= max_chars:
            return self.translate(text, source_lang, target_lang, quality, previous_context, glossary, session_id=session_id)
        
        # Chunk long text
        chunks = self._chunk_text(text, max_chars)
        translated_chunks = []
        total_cost = 0
        total_input = 0
        total_output = 0
        
        for i, chunk in enumerate(chunks):
            ctx = previous_context if i == 0 else None
            result = self.translate(chunk, source_lang, target_lang, quality, ctx, glossary, session_id=session_id)
            if not result.success:
                return result
            translated_chunks.append(result.translated_text)
            total_cost += result.estimated_cost
            total_input += result.input_tokens
            total_output += result.output_tokens
        
        return TranslationResult(
            translated_text="\n\n".join(translated_chunks),
            model_used=self.model_name,
            input_tokens=total_input,
            output_tokens=total_output,
            estimated_cost=total_cost,
            success=True,
        )

    def _chunk_text(self, text: str, max_chars: int) -> List[str]:
        """Split text into chunks at paragraph boundaries"""
        paragraphs = text.split("\n\n")
        chunks = []
        current = ""
        
        for para in paragraphs:
            if len(current) + len(para) + 2 <= max_chars:
                current += ("\n\n" if current else "") + para
            else:
                if current:
                    chunks.append(current)
                current = para
        
        if current:
            chunks.append(current)
        
        return chunks

    # ------------------------------------------------------------------
    # Novel-memory-aware translation
    # ------------------------------------------------------------------

    def _build_known_context(self, memory: "MemoryContext") -> str:
        """Compose the persistent-novel-knowledge block used in the prompt."""
        parts = []
        locked = memory.locked_block()
        if locked:
            parts.append(locked)
        if memory.general_instruction:
            parts.append(f"GENERAL NOVEL INSTRUCTION (always follow):\n{memory.general_instruction}")
        if memory.characters:
            parts.append(f"CHARACTERS (name / gender / flags / notes):\n{memory.characters}")
        if memory.terms:
            parts.append(f"TERMS / GLOSSARY (term -> translation, use exactly):\n{memory.terms}")
        if memory.plot:
            parts.append(f"OVERALL PLOT:\n{memory.plot}")
        if memory.arc_plot:
            parts.append(f"CURRENT ARC PLOT:\n{memory.arc_plot}")
        if memory.chapter_plot:
            parts.append(f"PREVIOUS CHAPTER PLOT:\n{memory.chapter_plot}")
        if memory.memory:
            parts.append(f"RUNNING MEMORY / NOTES:\n{memory.memory}")
        return "\n\n".join(parts) if parts else ""

    def translate_with_memory(
        self,
        text: str,
        source_lang: str,
        target_lang: str = "en",
        quality: Literal["fast", "balanced", "quality"] = "balanced",
        memory: Optional["MemoryContext"] = None,
        glossary: Optional[Dict[str, str]] = None,
        session_id: Optional[str] = None,
    ) -> "MemoryTranslationResult":
        """
        Translate a chapter using persistent per-novel memory.

        1. READS memory and injects it as context so names/gender/terms/plot
           stay consistent.
        2. TRANSLATES the chapter.
        3. UPDATES the memory from this chapter's content (new characters,
           gender reveals, terms, plot/arc/chapter summaries) so knowledge
           accumulates for the next chapter.
        """
        memory = memory or MemoryContext()
        known = self._build_known_context(memory)

        # Step 1+2: translate with memory as extra context
        prompt = self._build_prompt(
            text, source_lang, target_lang, quality,
            context=known or None,
            glossary=glossary or memory.terms_dict(),
        )
        try:
            generated = self._generate(prompt, session_id=session_id)
            translated = generated.strip()
            if not translated:
                return MemoryTranslationResult(
                    translated_text="", success=False,
                    error="Empty response from model during translation",
                    memory=memory,
                )
        except Exception as e:
            return MemoryTranslationResult(
                translated_text="", success=False, error=str(e), memory=memory,
            )

        # Step 3: ask the model to evolve the memory from this chapter
        try:
            update_text = self._memory_update_block(text, translated, memory, source_lang, target_lang, session_id=session_id)
            updated = self._parse_memory_update(update_text, memory)
        except Exception:
            # If memory update fails, keep translation (memory update is best-effort)
            updated = memory

        # Locked entries are user-authoritative — re-apply them over whatever the
        # model produced so the AI can never silently change a locked name/term.
        updated = self._reapply_locks(updated, memory)

        return MemoryTranslationResult(
            translated_text=translated,
            model_used=self.model_name,
            success=True,
            memory=updated,
        )

    def translate_with_memory_stream(
        self,
        text: str,
        source_lang: str,
        target_lang: str = "en",
        quality: Literal["fast", "balanced", "quality"] = "balanced",
        memory: Optional["MemoryContext"] = None,
        glossary: Optional[Dict[str, str]] = None,
        session_id: Optional[str] = None,
    ):
        """
        Stream translation deltas in real-time, then update per-novel memory.
        Yields StreamChunk(delta=...) for chunks, and StreamChunk(is_final=True, result=...) when complete.
        """
        memory = memory or MemoryContext()
        known = self._build_known_context(memory)
        prompt = self._build_prompt(
            text, source_lang, target_lang, quality,
            context=known or None,
            glossary=glossary or memory.terms_dict(),
        )
        accumulated = []
        try:
            for chunk in self._generate_stream(prompt, session_id=session_id):
                if chunk:
                    accumulated.append(chunk)
                    yield StreamChunk(delta=chunk, is_final=False)
        except Exception as e:
            logger.error(f"Stream generation error: {e}")
            yield StreamChunk(is_final=True, result=MemoryTranslationResult(
                translated_text="", success=False, error=str(e), memory=memory
            ))
            return

        translated = "".join(accumulated).strip()
        if not translated:
            yield StreamChunk(is_final=True, result=MemoryTranslationResult(
                translated_text="", success=False, error="Empty response from model stream", memory=memory
            ))
            return

        try:
            update_text = self._memory_update_block(text, translated, memory, source_lang, target_lang, session_id=session_id)
            updated = self._parse_memory_update(update_text, memory)
        except Exception:
            updated = memory

        updated = self._reapply_locks(updated, memory)

        final_res = MemoryTranslationResult(
            translated_text=translated,
            model_used=self.model_name,
            success=True,
            memory=updated,
        )
        yield StreamChunk(is_final=True, result=final_res)

    @staticmethod
    def _reapply_locks(updated: "MemoryContext", previous: "MemoryContext") -> "MemoryContext":
        """Force user-locked translations back into the updated memory.

        The model's freshly-learned `characters` / `terms` text MUST survive
        this pass: it is the ONLY way per-novel memory accumulates. So the
        locked entries are patched in line by line and every other line is left
        exactly as the model wrote it.

        (Rebuilding these fields wholesale from `glossary_entries` looks
        equivalent but is not: `_parse_memory_update` carries that list forward
        UNCHANGED from the previous chapter, so a rebuild replays a stale
        snapshot — the moment one entry was locked, every character learned
        afterwards was silently dropped for the rest of the novel.)
        """
        locked = previous.locked_entries()
        if not locked:
            return updated
        # Carry locked entries forward in the structured list (merge by source)
        merged = list(updated.glossary_entries or [])
        existing_sources = {e.get("source") for e in merged}
        for e in locked:
            if e.get("source") not in existing_sources:
                merged.append(dict(e))
        updated.glossary_entries = merged or None

        def _patch(text, entries, render):
            """Ensure each locked entry appears with its locked translation,
            touching only the one line that entry owns."""
            lines = (text or "").split("\n")
            for e in entries:
                tgt = (e.get("translated") or "").strip()
                if not tgt:
                    continue
                # Already correct somewhere? Leave the model's wording — and any
                # note it learned this chapter — completely untouched.
                if any(tgt in l for l in lines):
                    continue
                # The model used a DIFFERENT translation for this locked source:
                # overwrite that single line with the user's canonical form.
                src = (e.get("source") or "").strip()
                idx = next((i for i, l in enumerate(lines) if src and src in l), -1)
                if idx >= 0:
                    lines[idx] = render(e)
                else:
                    lines.append(render(e))
            return "\n".join(l for l in lines if l.strip())

        chars = [e for e in locked if e.get("type") != "term"]
        terms = [e for e in locked if e.get("type") == "term"]
        if chars:
            updated.characters = _patch(
                updated.characters, chars,
                lambda e: "{} ({}) - {}".format(
                    e.get("translated", ""), e.get("source", ""), e.get("note", "")).strip())
        if terms:
            updated.terms = _patch(
                updated.terms, terms,
                lambda e: "{} = {}".format(e.get("source", ""), e.get("translated", "")))
        return updated

    def _memory_update_block(
        self,
        source_text: str,
        translated_text: str,
        memory: "MemoryContext",
        source_lang: str,
        target_lang: str,
        session_id: Optional[str] = None,
    ) -> str:
        """Ask the model to produce the updated memory as a compact block."""
        prompt = f"""You maintain a knowledge file for a novel so future chapter translations stay consistent.

Current memory:
{self._build_known_context(memory) or '(empty)'}

Below are the SOURCE chapter and its TRANSLATION. Update the memory with these BOUNDED-SIZE rules
(critical for a 500+ chapter novel — the file must NOT grow unboundedly):

- characters: add/refine each NEW character that appears (name, gender, role, and ANY
  gender-bender / crossdressing / personality note). Keep prior entries UNLESS they are
  contradicted. One line per character, compact. If a character is clearly gone for good
  (dead / arc closed), you may merge them into one short "deceased/left" note instead of a
  full entry — but ONLY when the text confirms it.
- terms: add any recurring terms with their translation; keep prior terms (one line each).
- plot: RECURSIVE SUMMARIZATION — merge THIS chapter's developments into the existing plot
  summary. The output must be about the SAME LENGTH as the current plot (do not let it grow).
  Old detail gets compressed into broader strokes; keep only what matters for future chapters.
- arc_plot: the story arc this chapter belongs to + where it stands now. REPLACE, don't append.
- chapter_plot: a 1-3 sentence summary of ONLY this chapter. REPLACE the previous value.
- memory: fold this chapter's relevant facts into the notes list. HARD BUDGET: keep the whole
  `memory` field under ~800 words. When over budget, merge related facts, drop resolved/stale
  trivia, and keep only facts that will matter for FUTURE translations (relationships,
  foreshadowing, open plot threads, character states).

LOCKED entries (if any are listed above under USER-LOCKED NAMES/TERMS) are final —
do NOT alter their translations or remove them.

OUTPUT FORMAT — a fenced JSON object with exactly these keys, nothing else:
  "characters", "terms", "plot", "arc_plot", "chapter_plot", "memory"
All values are plain strings. Preserve honorifics and original-language names inside characters/terms.

SOURCE:
{source_text[:12000]}

TRANSLATION:
{translated_text[:12000]}
"""
        try:
            return self._generate(prompt, session_id=session_id)
        except Exception:
            return ""

    def needs_compaction(self, memory: "MemoryContext") -> bool:
        """Deprecated — moved to MemoryContext.needs_compaction(). Kept for safety."""
        return memory.needs_compaction()

    def compact_memory(self, memory: "MemoryContext", session_id: Optional[str] = None) -> "MemoryContext":
        """One-shot compaction: re-summarize the whole memory file WITHOUT a new chapter.

        Keeps characters/terms (one line each) and LOCKED glossary entries intact; merges
        plot/arc/notes into a tight summary. Returns a fresh MemoryContext."""
        locked_block = memory.locked_block()
        locked_instruction = ("USER-LOCKED NAMES/TERMS (keep exactly, never change):\n" + locked_block) if locked_block else ""
        prompt = f"""You are compressing a novel's translation-memory file. The file has grown
too large; compress it WITHOUT losing anything needed for future translations.

Current memory:
{self._build_known_context(memory) or '(empty)'}

Rules:
- characters: keep one compact line per character (name, gender, role, key traits).
  Merge deceased/closed-arc characters into a short "past" note.
- terms: keep all, one line each.
- plot: compress to at most ~200 words; keep major arcs, open threads, foreshadowing.
- arc_plot: current arc only, at most ~80 words.
- chapter_plot: drop it (set to '').
- memory: merge all notes into at most ~500 words; keep only facts needed for FUTURE
  translation consistency (relationships, character states, open plot threads).

{locked_instruction}

OUTPUT FORMAT — a fenced JSON object with exactly these keys, nothing else:
  "characters", "terms", "plot", "arc_plot", "chapter_plot", "memory"
All values are plain strings.
"""
        try:
            text = self._generate(prompt, session_id=session_id)
            if text:
                return self._parse_memory_update(text, memory)
        except Exception as e:
            logger.warning(f"memory compaction failed: {e}")
        return memory

def sync_glossary_entries(
    characters_text: str,
    terms_text: str,
    existing_entries: Optional[List[Dict]] = None,
) -> List[Dict]:
    """Parse characters and terms free-text and merge with existing glossary_entries.

    1. Preserves existing entries, including locked status, user edits, and custom notes.
    2. Adds newly discovered characters and terms from memory update.
    3. Updates empty fields in existing non-locked entries if new details were learned.
    """
    import re
    entries = [dict(e) for e in (existing_entries or [])]
    existing_by_src = {
        (e.get("type", "character"), (e.get("source") or "").strip().lower()): e
        for e in entries if e.get("source")
    }
    existing_by_trans = {
        (e.get("type", "character"), (e.get("translated") or "").strip().lower()): e
        for e in entries if e.get("translated")
    }

    # 1. Parse Characters
    if characters_text:
        anchors = list(re.finditer(r"([^;()\n]{1,80}?)\s*\(([^()]+)\)\s*[-–:]\s*", characters_text))
        for i, m in enumerate(anchors):
            end = anchors[i + 1].start() if i + 1 < len(anchors) else len(characters_text)
            note = characters_text[m.end():end].strip().rstrip(";").strip()
            translated = m.group(1).strip()
            src = m.group(2).strip()
            aliases = re.split(r"\s*/\s*", translated)
            primary = aliases[0].strip()
            src_aliases = re.split(r"\s*/\s*", src) if "/" in src else []
            src_primary = src_aliases[0].strip() if src_aliases else src
            if not primary and not src_primary:
                continue

            k_src = ("character", src_primary.lower()) if src_primary else None
            k_trans = ("character", primary.lower()) if primary else None
            matched = (k_src and existing_by_src.get(k_src)) or (k_trans and existing_by_trans.get(k_trans))

            if matched:
                if not matched.get("locked"):
                    if primary and not matched.get("translated"):
                        matched["translated"] = primary
                    if note and not matched.get("note"):
                        matched["note"] = note
            else:
                new_entry = {
                    "type": "character",
                    "translated": primary,
                    "source": src_primary,
                    "note": note,
                    "locked": False,
                }
                entries.append(new_entry)
                if k_src:
                    existing_by_src[k_src] = new_entry
                if k_trans:
                    existing_by_trans[k_trans] = new_entry

    # 2. Parse Terms
    if terms_text:
        for line in terms_text.splitlines():
            line = line.strip()
            if not line:
                continue
            m1 = re.match(r"^([^()]{1,80}?)\s*\(([^()]+)\)\s*[-–:]\s*(.*)$", line)
            m2 = re.match(r"^(.*?)\s*(?:=|->|→)\s*(.*)$", line)
            if m1:
                translated = m1.group(1).strip()
                source = m1.group(2).strip()
                note = m1.group(3).strip()
            elif m2:
                source = m2.group(1).strip()
                translated = m2.group(2).strip()
                note = ""
            else:
                continue

            if not source and not translated:
                continue

            k_src = ("term", source.lower()) if source else None
            k_trans = ("term", translated.lower()) if translated else None
            matched = (k_src and existing_by_src.get(k_src)) or (k_trans and existing_by_trans.get(k_trans))

            if matched:
                if not matched.get("locked"):
                    if translated and not matched.get("translated"):
                        matched["translated"] = translated
                    if note and not matched.get("note"):
                        matched["note"] = note
            else:
                new_entry = {
                    "type": "term",
                    "source": source,
                    "translated": translated,
                    "note": note,
                    "locked": False,
                }
                entries.append(new_entry)
                if k_src:
                    existing_by_src[k_src] = new_entry
                if k_trans:
                    existing_by_trans[k_trans] = new_entry

    return entries


    @staticmethod
    def _parse_memory_update(text: str, memory: "MemoryContext"):
        """Tolerant JSON extraction for the memory-update block."""
        import json as _json
        import re as _re
        try:
            # Try to extract the fenced JSON
            m = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, _re.DOTALL)
            if m:
                data = _json.loads(m.group(1))
            else:
                data = _json.loads(text)
            if not isinstance(data, dict):
                return memory
        except Exception:
            return memory

        def _get(key):
            val = data.get(key)
            return val.strip() if isinstance(val, str) else (memory.__dict__.get(key) or "")

        chars = _get("characters")
        terms = _get("terms")
        synced_glossary = sync_glossary_entries(chars, terms, memory.glossary_entries)

        updated = MemoryContext(
            general_instruction=memory.general_instruction,
            characters=chars,
            terms=terms,
            plot=_get("plot"),
            arc_plot=_get("arc_plot"),
            chapter_plot=_get("chapter_plot"),
            memory=_get("memory"),
            glossary_entries=synced_glossary or memory.glossary_entries,
        )
        return updated


@dataclass
class MemoryContext:
    """Structured per-novel knowledge fed to (and maintained by) the translator."""
    general_instruction: str = ""
    characters: str = ""
    terms: str = ""
    plot: str = ""
    arc_plot: str = ""
    chapter_plot: str = ""
    memory: str = ""
    # Structured entries: [{"type":"character"|"term","source":..., "translated":...,
    #   "note":..., "locked": bool}] — locked entries must NEVER change.
    glossary_entries: Optional[List[Dict]] = None

    def locked_entries(self) -> List[Dict]:
        if not self.glossary_entries:
            return []
        return [e for e in self.glossary_entries if e.get("locked")]

    def locked_block(self) -> str:
        """Text block of user-locked names/terms the model must use exactly."""
        locked = self.locked_entries()
        if not locked:
            return ""
        lines = []
        for e in locked:
            src = e.get("source", "")
            tgt = e.get("translated", "")
            if src and tgt:
                lines.append(f"- {src} = {tgt}")
            elif tgt:
                lines.append(f"- {tgt}")
        if not lines:
            return ""
        return (
            "USER-LOCKED NAMES/TERMS (MANDATORY, NEVER change or re-translate these; "
            "use exactly these translations every time):\n" + "\n".join(lines)
        )

    def terms_dict(self) -> Optional[Dict[str, str]]:
        """Best-effort parse of multi-line terms and locked entries into a dict."""
        import re
        result = {}
        for e in self.locked_entries():
            src = (e.get("source") or "").strip()
            tgt = (e.get("translated") or "").strip()
            if src and tgt:
                result[src] = tgt

        if self.terms:
            for line in self.terms.splitlines():
                line = line.strip()
                if not line:
                    continue
                m1 = re.match(r"^([^()]{1,80}?)\s*\(([^()]+)\)\s*[-–:]\s*", line)
                if m1:
                    tgt = m1.group(1).strip()
                    src = m1.group(2).strip()
                    if src and tgt and src not in result:
                        result[src] = tgt
                    continue
                if "->" in line:
                    k, v = line.split("->", 1)
                    k_s, v_s = k.strip(), v.strip()
                    if k_s and v_s and k_s not in result:
                        result[k_s] = v_s
                elif "→" in line:
                    k, v = line.split("→", 1)
                    k_s, v_s = k.strip(), v.strip()
                    if k_s and v_s and k_s not in result:
                        result[k_s] = v_s
                elif "=" in line:
                    k, v = line.split("=", 1)
                    k_s, v_s = k.strip(), v.strip()
                    if k_s and v_s and k_s not in result:
                        result[k_s] = v_s
        return result or None

    def needs_compaction(self) -> bool:
        """True when the memory has grown past its budget and needs a compaction pass."""
        total = (len(self.characters or "") + len(self.terms or "")
                 + len(self.plot or "") + len(self.arc_plot or "")
                 + len(self.memory or ""))
        return total > 6000  # ~1500 tokens of context is the soft budget


@dataclass
class MemoryTranslationResult:
    translated_text: str
    success: bool
    error: Optional[str] = None
    model_used: str = ""
    memory: MemoryContext = None


# Singleton instance
_translator_instance: Optional[GeminiTranslator] = None


class OpenAIRelayTranslator(GeminiTranslator):
    """
    Drop-in GeminiTranslator that talks to an OpenAI-compatible chat/completions
    endpoint instead of the Gemini API. Used as a fallback when the Gemini
    quota is exhausted. Inherits all prompt/memory logic; only `_generate`
    differs.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        temperature: float = 0.3,
        session_id: Optional[str] = None,
    ):
        # A base class invariant is that a `model` attribute + `pricing` exist.
        self.api_key = api_key or os.getenv("FALLBACK_API_KEY")
        self.base_url = (base_url or os.getenv("FALLBACK_BASE_URL") or "https://api.relay.example.com/v1").rstrip("/")
        self.model_name = model or os.getenv("FALLBACK_MODEL") or "deepseek-v4-flash"
        self.temperature = temperature
        self.model = None  # not used; kept for interface parity
        self.pricing = {"input": 0.10, "output": 0.40}  # rough estimate
        self.session_id = session_id or f"nyaa-sess-{uuid.uuid4().hex[:12]}"
        if not self.api_key:
            raise ValueError("Fallback API key required (FALLBACK_API_KEY)")

    def _generate(self, prompt: str, session_id: Optional[str] = None, **kwargs) -> str:
        """POST the prompt to the OpenAI-compatible relay via call_ai_provider.

        Retries transient failures (empty reply, 429 rate-limit, 5xx) with a
        short exponential backoff. Hard failures (401/403 auth) raise
        RelayAuthError immediately so callers can stop work instead of
        hammering a dead key."""
        import time as _time
        last_err = None
        for attempt in range(3):
            if attempt > 0:
                # backoff: 2s, 4s for transient (rate-limit / server) errors
                _time.sleep(2 * attempt)
            payload = {
                "model": self.model_name,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": self.temperature,
                "max_tokens": 8192,
            }
            try:
                data = call_ai_provider(
                    base_url=self.base_url,
                    endpoint="/chat/completions",
                    api_key=self.api_key,
                    payload=payload,
                    session_id=session_id or self.session_id,
                    timeout=180,
                )
                try:
                    content = data["choices"][0]["message"].get("content") or ""
                except (KeyError, IndexError, TypeError):
                    raise RuntimeError(f"Unexpected relay response: {str(data)[:300]}")
                if content.strip():
                    return content
                last_err = "empty content from relay"
                logger.warning(f"relay returned empty content (attempt {attempt + 1})")
            except urllib.error.HTTPError as e:
                # 401/403 = key disabled/invalid on the router dashboard — a
                # PERMANENT failure. Don't retry, don't fall back (same key),
                # just stop so a batch doesn't fail every chapter.
                if e.code in (401, 403):
                    logger.error(f"relay auth rejected (HTTP {e.code}) — key disabled or invalid")
                    raise RelayAuthError(f"Relay rejected the API key (HTTP {e.code}). "
                                         f"Check the key in the AI router dashboard / Settings.")
                last_err = f"relay HTTP {e.code}"
                logger.warning(f"relay call failed (attempt {attempt + 1}): HTTP {e.code}")
            except RelayAuthError:
                raise
            except Exception as e:
                last_err = str(e)
                logger.warning(f"relay call failed (attempt {attempt + 1}): {e}")
        raise RuntimeError(f"Relay returned no content: {last_err}")

    def _generate_stream(self, prompt: str, session_id: Optional[str] = None, **kwargs):
        """POST prompt with stream=True via call_ai_provider and yield tokens/chunks."""
        payload = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
            "max_tokens": 8192,
            "stream": True,
        }
        try:
            for delta in call_ai_provider(
                base_url=self.base_url,
                endpoint="/chat/completions",
                api_key=self.api_key,
                payload=payload,
                session_id=session_id or self.session_id,
                timeout=180,
                stream=True,
            ):
                if delta:
                    yield delta
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise RelayAuthError(f"Relay rejected the API key (HTTP {e.code})")
            yield self._generate(prompt, session_id=session_id)
        except Exception as e:
            logger.warning(f"relay streaming error ({e}), falling back to standard generate")
            yield self._generate(prompt, session_id=session_id)


class FallbackTranslator:
    """Wraps a primary translator with an ordered chain of fallbacks. On any
    failure (quota, network, empty reply, success=False) it walks the chain:
    primary -> fallback[0] -> fallback[1] -> ... until one succeeds."""

    def __init__(self, primary: GeminiTranslator, fallbacks: Optional[List[GeminiTranslator]] = None):
        self.primary = primary
        self.fallbacks = fallbacks or []
        if not self.fallbacks:
            try:
                self.fallbacks = [OpenAIRelayTranslator()]
            except Exception as e:
                logger.error(f"Relay fallback unavailable: {e}")

    def _run(self, method: str, *args, **kwargs):
        """Run method on primary; on failure, walk the fallback chain."""
        chain = [self.primary] + list(self.fallbacks)
        last_error = None
        result = None
        for i, translator in enumerate(chain):
            if translator is None:
                continue
            try:
                result = getattr(translator, method)(*args, **kwargs)
                if getattr(result, "success", True):
                    return result
                last_error = getattr(result, "error", None) or "success=False"
            except RelayAuthError as e:
                # Key rejected permanently — the fallback chain shares the SAME
                # key, so don't try them. Propagate so callers stop work now.
                logger.error(f"Relay auth failure on translator #{i}: {e}")
                raise
            except Exception as e:
                last_error = str(e)
            if i < len(chain) - 1:
                logger.warning(f"Translator #{i} ({type(translator).__name__}) failed ({last_error}); trying next")
        if last_error:
            logger.error(f"All translators failed; last error: {last_error}")
        return result

    def translate(self, *args, **kwargs) -> TranslationResult:
        return self._run("translate", *args, **kwargs)

    def translate_chapter(self, *args, **kwargs) -> TranslationResult:
        return self._run("translate_chapter", *args, **kwargs)

    def translate_short(self, *args, **kwargs) -> str:
        return self._run("translate_short", *args, **kwargs)

    def translate_with_memory(self, *args, **kwargs) -> "MemoryTranslationResult":
        return self._run("translate_with_memory", *args, **kwargs)

    def translate_with_memory_stream(self, *args, **kwargs):
        """Stream translation on primary translator, walking fallback chain on failure."""
        chain = [self.primary] + list(self.fallbacks)
        last_error = None
        for i, translator in enumerate(chain):
            if translator is None:
                continue
            try:
                gen = translator.translate_with_memory_stream(*args, **kwargs)
                accumulated_chunks = []
                success = False
                for chunk in gen:
                    if getattr(chunk, "is_final", False):
                        res = getattr(chunk, "result", None)
                        if res and getattr(res, "success", True):
                            success = True
                            for prev_chunk in accumulated_chunks:
                                yield prev_chunk
                            yield chunk
                            return
                        else:
                            last_error = getattr(res, "error", None) or "stream translation failed"
                            break
                    else:
                        accumulated_chunks.append(chunk)
                if not success and accumulated_chunks:
                    # If generator finished without is_final but yielded deltas
                    for prev_chunk in accumulated_chunks:
                        yield prev_chunk
                    return
            except RelayAuthError as e:
                logger.error(f"Relay auth failure on translator #{i}: {e}")
                raise
            except Exception as e:
                last_error = str(e)
                logger.warning(f"Streaming on translator #{i} ({type(translator).__name__}) failed ({e}); trying next")
        if last_error:
            logger.error(f"All streaming translators failed; last error: {last_error}")
            yield StreamChunk(is_final=True, result=MemoryTranslationResult(
                translated_text="", success=False, error=last_error, memory=kwargs.get("memory")
            ))

    # Memory-compaction helpers: forward to the first translator in the chain
    # that exposes them (the primary has them; fallbacks inherit from GeminiTranslator).
    def compact_memory(self, *args, **kwargs):
        for translator in [self.primary] + list(self.fallbacks):
            if translator is None:
                continue
            if hasattr(translator, "compact_memory"):
                return translator.compact_memory(*args, **kwargs)
        raise AttributeError("No translator exposes compact_memory")

    def needs_compaction(self, *args, **kwargs) -> bool:
        return self.primary.needs_compaction(*args, **kwargs)

    async def translate_async(self, *args, **kwargs) -> TranslationResult:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._run, "translate", *args, **kwargs)


def get_translator(
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> GeminiTranslator:
    """Get or create translator instance.

    Two-model fallback chain (2026-08-19):
      1. Model 1 (deepseek-v4-flash) - primary, best value
      2. Model 2 (gpt-5.6-luna) - quality tier
         - Can share Model 1's URL/key (default)
         - Or use separate URL/key via FALLBACK_2_BASE_URL, FALLBACK_2_API_KEY
    Each step only engages when the previous one fails.
    """
    global _translator_instance
    if _translator_instance is None:
        # Model 1: primary relay
        relay_key = os.getenv("FALLBACK_API_KEY")
        relay_base_url = os.getenv("FALLBACK_BASE_URL") or "https://opencode.ai/zen/go/v1"
        primary_model = os.getenv("FALLBACK_MODEL") or "deepseek-v4-flash"
        
        try:
            primary = OpenAIRelayTranslator(
                api_key=relay_key, model=primary_model, base_url=relay_base_url)
        except Exception as e:
            logger.error(f"Model 1 translator init failed ({e})")
            return None
        
        fallbacks = []
        
        # Model 2: quality tier (can share or use separate URL/key)
        m2 = os.getenv("FALLBACK_MODEL_2")
        if m2:
            m2_base = os.getenv("FALLBACK_2_BASE_URL") or relay_base_url
            m2_key = os.getenv("FALLBACK_2_API_KEY") or relay_key
            try:
                fallbacks.append(OpenAIRelayTranslator(
                    api_key=m2_key, model=m2, base_url=m2_base))
                logger.info(f"Model 2: {m2} @ {m2_base}")
            except Exception as e:
                logger.error(f"Model 2 translator {m2} init failed: {e}")
        
        _translator_instance = FallbackTranslator(primary, fallbacks=fallbacks)
    return _translator_instance
