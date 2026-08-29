"""
backend/translator.py's _reapply_locks: it must patch locked glossary entries
back in LINE BY LINE, never rebuild `characters`/`terms` wholesale from
`glossary_entries`. That list is carried forward UNCHANGED from the previous
chapter by _parse_memory_update, so a wholesale rebuild replays a stale
snapshot — the moment one entry was locked, every character the model learned
in every chapter AFTER that was silently discarded for the rest of the novel.
"""
from translator import GeminiTranslator, MemoryContext


def _memory_with_lock():
    return MemoryContext(
        characters="Angelia (安潔莉雅) - the princess",
        terms="魔力 = mana",
        glossary_entries=[
            {"type": "character", "source": "安潔莉雅", "translated": "Angelia",
             "note": "the princess", "locked": True},
        ],
    )


def test_newly_learned_character_survives_reapply_locks():
    previous = _memory_with_lock()
    updated = MemoryContext(
        characters=("Angelia (安潔莉雅) - the princess, now crowned\n"
                   "Boran (博兰) - new knight introduced this chapter"),
        terms="魔力 = mana\n剑气 = sword aura",
        glossary_entries=list(previous.glossary_entries),  # carried forward, as the real code does
    )

    out = GeminiTranslator._reapply_locks(updated, previous)

    assert "Boran" in out.characters, "a character learned AFTER the first lock must not be dropped"
    assert "now crowned" in out.characters, "a note learned this chapter must survive too"
    assert "Angelia" in out.characters
    assert "sword aura" in out.terms, "a term learned after the lock must survive"


def test_a_locked_entry_that_the_model_renamed_is_corrected_without_losing_other_entries():
    previous = _memory_with_lock()
    renamed = MemoryContext(
        characters="Angelica (安潔莉雅) - the princess\nBoran (博兰) - new knight",
        terms="魔力 = mana",
        glossary_entries=list(previous.glossary_entries),
    )

    out = GeminiTranslator._reapply_locks(renamed, previous)

    assert "Angelia (" in out.characters, "the locked translation must be restored over the model's rename"
    assert "Angelica" not in out.characters
    assert "Boran" in out.characters, "an unrelated new character must not be collateral damage"


def test_no_locks_leaves_memory_untouched():
    updated = MemoryContext(characters="whatever the model wrote")
    out = GeminiTranslator._reapply_locks(updated, MemoryContext())
    assert out is updated
