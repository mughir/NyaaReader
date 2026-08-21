"""
Translation Quality Assessment System for NyaaReader

Provides AI-powered quality rating and translation theme support
for enhanced translation quality and user customization.
"""
import asyncio
import hashlib
import logging
import time
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger("novel-reader.translation_quality")


class TranslationQuality(Enum):
    """Translation quality levels."""
    EXCELLENT = "excellent"
    GOOD = "good"
    FAIR = "fair"
    POOR = "poor"
    NEEDS_REVISION = "needs_revision"


class TranslationQualityIssue(Enum):
    """Types of quality issues detected in translations."""
    LENGTH_ANOMALY = "length_anomaly"
    REPETITIVE_CONTENT = "repetitive_content"
    LANGUAGE_INCONSISTENCY = "language_inconsistency"
    CULTURAL_ADAPTATION_NEEDED = "cultural_adaptation_needed"
    INFORMAL_TONE = "informal_tone"
    FORMAL_TONE_NEEDED = "formal_tone_needed"


@dataclass
class QualityAssessment:
    """Result of translation quality assessment."""
    quality_level: TranslationQuality
    quality_score: float  # 0.0 to 1.0
    issues: List[TranslationQualityIssue]
    suggestions: List[str]
    severity: float  # 0.0 to 1.0 (impact on readability)


class TranslationQualityIndicator:
    """
    AI-powered translation quality assessment system.
    
    Analyzes translations for quality, detects issues, and provides
    suggestions for improvement.
    """
    
    def __init__(self):
        self.quality_cache: Dict[str, QualityAssessment] = {}
        self.quality_thresholds = {
            TranslationQuality.EXCELLENT: 0.9,
            TranslationQuality.GOOD: 0.8,
            TranslationQuality.FAIR: 0.7,
            TranslationQuality.POOR: 0.6,
            TranslationQuality.NEEDS_REVISION: 0.0
        }
        self.pattern_cache: Dict[str, List[str]] = {}
    
    def assess_translation_quality(
        self,
        translated_text: str,
        original_text: str,
        source_language: str,
        target_language: str,
        context: Optional[Dict[str, Any]] = None
    ) -> QualityAssessment:
        """
        Assess the quality of a translation.
        
        Args:
            translated_text: The translated text to assess
            original_text: The original text for comparison
            source_language: Source language code
            target_language: Target language code
            context: Additional context (genre, proper nouns, etc.)
            
        Returns:
            QualityAssessment with score and issues
        """
        # Generate cache key
        cache_key = self._generate_cache_key(
            translated_text, original_text, source_language, target_language, context
        )
        
        # Check cache first
        if cache_key in self.quality_cache:
            return self.quality_cache[cache_key]
        
        # Perform quality assessment
        quality_score = self._calculate_quality_score(
            translated_text, original_text, source_language, target_language, context
        )
        
        issues = self._detect_quality_issues(
            translated_text, original_text, source_language, target_language, context
        )
        
        suggestions = self._generate_quality_suggestions(
            translated_text, original_text, source_language, target_language, issues
        )
        
        quality_level = self._score_to_quality_level(quality_score)
        severity = self._calculate_severity(issues)
        
        assessment = QualityAssessment(
            quality_level=quality_level,
            quality_score=quality_score,
            issues=issues,
            suggestions=suggestions,
            severity=severity
        )
        
        # Cache the result
        self.quality_cache[cache_key] = assessment
        
        # Maintain cache size
        if len(self.quality_cache) > 1000:
            oldest_key = next(iter(self.quality_cache))
            del self.quality_cache[oldest_key]
        
        return assessment
    
    def _generate_cache_key(
        self,
        translated_text: str,
        original_text: str,
        source_language: str,
        target_language: str,
        context: Optional[Dict[str, Any]]
    ) -> str:
        """Generate a unique cache key for the assessment."""
        key_components = [
            translated_text,
            original_text,
            source_language,
            target_language,
            str(sorted(context.items())) if context else ""
        ]
        return hashlib.md5("|".join(key_components).encode()).hexdigest()
    
    def _calculate_quality_score(
        self,
        translated_text: str,
        original_text: str,
        source_language: str,
        target_language: str,
        context: Optional[Dict[str, Any]]
    ) -> float:
        """Calculate a comprehensive quality score."""
        if not translated_text or not original_text:
            return 0.0
        
        scores = []
        
        # Length appropriateness score
        length_score = self._calculate_length_score(translated_text, original_text)
        scores.append(("length", length_score, 0.2))
        
        # Content preservation score
        preservation_score = self._calculate_preservation_score(translated_text, original_text)
        scores.append(("preservation", preservation_score, 0.25))
        
        # Linguistic fluency score
        fluency_score = self._calculate_fluency_score(translated_text, target_language)
        scores.append(("fluency", fluency_score, 0.2))
        
        # Cultural adaptation score
        if context:
            cultural_score = self._calculate_cultural_score(translated_text, source_language, target_language, context)
            scores.append(("cultural", cultural_score, 0.15))
        else:
            scores.append(("cultural", 0.5, 0.15))
        
        # Technical correctness score (simplified)
        correctness_score = self._calculate_correctness_score(translated_text, target_language)
        scores.append(("correctness", correctness_score, 0.2))
        
        # Calculate weighted average
        total_score = 0.0
        total_weight = 0.0
        
        for name, score, weight in scores:
            if score > 0:  # Only count non-zero scores
                total_score += score * weight
                total_weight += weight
        
        return total_score / total_weight if total_weight > 0 else 0.0
    
    def _calculate_length_score(self, translated: str, original: str) -> float:
        """Calculate if translation length is appropriate."""
        original_words = len(original.split())
        translated_words = len(translated.split())
        
        if original_words == 0:
            return 1.0
        
        ratio = translated_words / original_words
        
        # Ideal ratio is between 0.6 and 2.0 (accounting for language differences)
        if 0.6 <= ratio <= 2.0:
            return 1.0
        elif ratio < 0.6:
            return ratio / 0.6  # Penalize too short
        else:
            return 1.0 / (ratio * 0.5)  # Penalize too long
    
    def _calculate_preservation_score(self, translated: str, original: str) -> float:
        """Calculate how well content is preserved."""
        original_words = set(original.lower().split())
        translated_words = set(translated.lower().split())
        
        if not original_words:
            return 1.0
        
        # Calculate overlap (Jaccard similarity)
        intersection = original_words & translated_words
        union = original_words | translated_words
        
        if not union:
            return 0.0
        
        return len(intersection) / len(union)
    
    def _calculate_fluency_score(self, text: str, language: str) -> float:
        """Calculate linguistic fluency."""
        if not text:
            return 0.0
        
        # Basic fluency checks
        score = 1.0
        
        # Check for excessive repetition
        words = text.split()
        if len(words) > 10:
            unique_ratio = len(set(words)) / len(words)
            if unique_ratio < 0.3:  # Too repetitive
                score *= 0.7
        
        # Check for very long sentences (might indicate poor flow)
        sentences = [s.strip() for s in text.split('.') if s.strip()]
        if sentences:
            avg_sentence_length = sum(len(s.split()) for s in sentences) / len(sentences)
            if avg_sentence_length > 30:  # Very long sentences
                score *= 0.8
        
        # Check for common grammar issues (simplified)
        grammar_issues = self._detect_grammar_issues(text, language)
        if grammar_issues:
            score *= (1.0 - len(grammar_issues) * 0.1)
        
        return max(0.0, min(1.0, score))
    
    def _calculate_cultural_score(
        self,
        translated: str,
        source_lang: str,
        target_lang: str,
        context: Dict[str, Any]
    ) -> float:
        """Calculate cultural adaptation appropriateness."""
        score = 1.0
        
        # Check if cultural references are adapted
        if source_lang != target_lang:
            # Simple check for cultural adaptation
            if source_lang == 'ja' and target_lang == 'en':
                # Japanese to English: check for unadapted honorifics
                if any(word in translated.lower() for word in ['san', 'kun', 'chan', 'sama']):
                    score *= 0.8  # Not well adapted
        
        # Check if context-specific terms are preserved
        if context.get('domain') == 'light_novel':
            # Light novel terms should be adapted appropriately
            light_novel_terms = ['-sensei', 'desu', 'masu', 'ga', 'wa']
            if any(term in translated.lower() for term in light_novel_terms):
                score *= 0.9  # Partially adapted
        
        return max(0.0, min(1.0, score))
    
    def _calculate_correctness_score(self, text: str, language: str) -> float:
        """Calculate technical correctness."""
        score = 1.0
        
        # Check for basic spelling/typing issues (simplified)
        words = text.split()
        if len(words) > 5:
            # Look for obviously malformed words
            malformed_count = sum(1 for word in words if len(word) > 20 or not word.isalnum())
            if malformed_count > 0:
                score *= (1.0 - malformed_count / len(words) * 0.5)
        
        return max(0.0, min(1.0, score))
    
    def _detect_grammar_issues(self, text: str, language: str) -> List[str]:
        """Detect potential grammar issues."""
        issues = []
        
        # Simplified grammar checks
        if language == 'en':
            # Check for common patterns
            if text.count('  ') > len(text) * 0.1:  # Too many double spaces
                issues.append('excessive_spacing')
            
            # Check for ending with prepositions
            if text.strip().endswith(' ') and text.strip()[-1] in [' ', ',', ';']:
                pass  # Not necessarily an issue
        
        return issues
    
    def _score_to_quality_level(self, score: float) -> TranslationQuality:
        """Convert numeric score to quality level."""
        for quality_level, threshold in sorted(
            self.quality_thresholds.items(),
            key=lambda x: x[1],
            reverse=True
        ):
            if score >= threshold:
                return quality_level
        
        return TranslationQuality.NEEDS_REVISION
    
    def _calculate_severity(self, issues: List[TranslationQualityIssue]) -> float:
        """Calculate severity based on issues."""
        severity_map = {
            TranslationQualityIssue.LENGTH_ANOMALY: 0.3,
            TranslationQualityIssue.REPETITIVE_CONTENT: 0.4,
            TranslationQualityIssue.LANGUAGE_INCONSISTENCY: 0.5,
            TranslationQualityIssue.CULTURAL_ADAPTATION_NEEDED: 0.6,
            TranslationQualityIssue.INFORMAL_TONE: 0.2,
            TranslationQualityIssue.FORMAL_TONE_NEEDED: 0.2,
        }
        
        total_severity = 0.0
        for issue in issues:
            total_severity += severity_map.get(issue, 0.3)
        
        return min(1.0, total_severity / len(issues) if issues else 0.0)
    
    def _detect_quality_issues(
        self,
        translated: str,
        original: str,
        source_lang: str,
        target_lang: str,
        context: Optional[Dict[str, Any]]
    ) -> List[TranslationQualityIssue]:
        """Detect quality issues in translation."""
        issues = []
        
        # Check length anomaly
        original_len = len(original)
        translated_len = len(translated)
        if original_len > 0:
            ratio = translated_len / original_len
            if ratio > 2.5 or ratio < 0.4:
                issues.append(TranslationQualityIssue.LENGTH_ANOMALY)
        
        # Check for repetitive content
        words = translated.split()
        if len(words) > 10:
            unique_ratio = len(set(words)) / len(words)
            if unique_ratio < 0.3:
                issues.append(TranslationQualityIssue.REPETITIVE_CONTENT)
        
        # Check language consistency
        if source_lang == target_lang and translated.lower() != original.lower():
            issues.append(TranslationQualityIssue.LANGUAGE_INCONSISTENCY)
        
        # Check cultural adaptation
        if source_lang != target_lang:
            if self._needs_cultural_adaptation(translated, source_lang, target_lang):
                issues.append(TranslationQualityIssue.CULTURAL_ADAPTATION_NEEDED)
        
        # Check tone appropriateness
        if self._needs_formal_tone(translated):
            issues.append(TranslationQualityIssue.FORMAL_TONE_NEEDED)
        elif self._needs_informal_tone(translated):
            issues.append(TranslationQualityIssue.INFORMAL_TONE)
        
        return issues
    
    def _needs_cultural_adaptation(
        self,
        text: str,
        source_lang: str,
        target_lang: str
    ) -> bool:
        """Check if cultural adaptation is needed."""
        if source_lang == 'ja' and target_lang == 'en':
            # Japanese honorifics should be adapted
            honorifics = ['san', 'kun', 'chan', 'sama', 'sensei', 'sempai', 'kohei']
            return any(honorific in text.lower() for honorific in honorifics)
        
        return False
    
    def _needs_formal_tone(self, text: str) -> bool:
        """Check if text should be more formal."""
        formal_indicators = [
            'mr.', 'mrs.', 'ms.', 'dr.', 'prof.', 'sir', 'madam',
            'please', 'thank you', 'sorry'
        ]
        return any(indicator in text.lower() for indicator in formal_indicators)
    
    def _needs_informal_tone(self, text: str) -> bool:
        """Check if text should be more informal."""
        informal_indicators = [
            'hey', 'hi', 'hello', 'hey there', 'yo', 'sup',
            'gonna', 'wanna', 'gotta'
        ]
        return any(indicator in text.lower() for indicator in informal_indicators)
    
    def _generate_quality_suggestions(
        self,
        translated: str,
        original: str,
        source_lang: str,
        target_lang: str,
        issues: List[TranslationQualityIssue]
    ) -> List[str]:
        """Generate suggestions for improving translation quality."""
        suggestions = []
        
        for issue in issues:
            if issue == TranslationQualityIssue.LENGTH_ANOMALY:
                original_len = len(original)
                translated_len = len(translated)
                ratio = translated_len / original_len if original_len > 0 else 1
                
                if ratio > 2.0:
                    suggestions.append("Translation is too long. Consider condensing.")
                elif ratio < 0.5:
                    suggestions.append("Translation is too short. Add more detail.")
                
            elif issue == TranslationQualityIssue.REPETITIVE_CONTENT:
                suggestions.append("Reduce repetition by using synonyms or varied sentence structures.")
                
            elif issue == TranslationQualityIssue.LANGUAGE_INCONSISTENCY:
                if source_lang == target_lang:
                    suggestions.append("Same language but different content. Check for translation accuracy.")
                
            elif issue == TranslationQualityIssue.CULTURAL_ADAPTATION_NEEDED:
                if source_lang == 'ja' and target_lang == 'en':
                    suggestions.append("Adapt Japanese honorifics (san, kun, etc.) to appropriate English equivalents.")
                
            elif issue == TranslationQualityIssue.FORMAL_TONE_NEEDED:
                suggestions.append("Consider using more formal language for professional or respectful contexts.")
                
            elif issue == TranslationQualityIssue.INFORMAL_TONE:
                suggestions.append("Make the translation more conversational and less formal.")
        
        return suggestions


class TranslationTheme:
    """
    User-customizable translation styles and preferences.
    
    Allows users to define translation themes with specific preferences
    for style, tone, and cultural adaptation.
    """
    
    def __init__(self, name: str):
        self.name = name
        self.preferences = self._get_default_preferences()
        self.quality_threshold = 0.7
        self.custom_glossary: Dict[str, str] = {}
        self.excluded_terms: List[str] = []
    
    def _get_default_preferences(self) -> Dict[str, Any]:
        """Get default theme preferences."""
        return {
            'formal_level': 0.5,  # 0=formal, 1=casual
            'preservation_level': 0.8,
            'translation_tone': 'natural',  # natural, literary, conversational
            'allow_literal_translation': True,
            'cultural_adaptation': True,
            'respect_honorifics': True,
            'style_consistency': True,
            'auto_quality_check': False,
        }
    
    def update_preferences(self, preferences: Dict[str, Any]):
        """Update theme preferences."""
        self.preferences.update(preferences)
    
    def add_to_glossary(self, source_term: str, target_term: str, context: str = ""):
        """Add a term to the theme's glossary."""
        self.custom_glossary[source_term] = {
            'translation': target_term,
            'context': context,
            'added_date': time.time()
        }
    
    def remove_from_glossary(self, source_term: str):
        """Remove a term from the theme's glossary."""
        self.custom_glossary.pop(source_term, None)
    
    def add_excluded_term(self, term: str):
        """Add a term to exclude from translation."""
        self.excluded_terms.append(term)
    
    def remove_excluded_term(self, term: str):
        """Remove a term from exclusions."""
        if term in self.excluded_terms:
            self.excluded_terms.remove(term)
    
    def apply_theme(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        quality_indicator: TranslationQualityIndicator
    ) -> Tuple[str, List[str]]:
        """
        Apply theme-specific modifications to translation.
        
        Returns:
            Tuple of (modified_text, applied_rules)
        """
        modified_text = text
        applied_rules = []
        
        # Apply style modifications
        if self.preferences['formal_level'] > 0.7:
            modified_text = self._apply_formal_style(modified_text)
            applied_rules.append("formal_style")
        elif self.preferences['formal_level'] < 0.3:
            modified_text = self._apply_casual_style(modified_text)
            applied_rules.append("casual_style")
        
        # Apply translation tone
        tone = self.preferences['translation_tone']
        if tone == 'literary':
            modified_text = self._apply_literary_tone(modified_text)
            applied_rules.append("literary_tone")
        elif tone == 'conversational':
            modified_text = self._apply_conversational_tone(modified_text)
            applied_rules.append("conversational_tone")
        
        # Apply cultural adaptation
        if self.preferences['cultural_adaptation']:
            modified_text = self._apply_cultural_adaptation(
                modified_text, source_lang, target_lang
            )
            applied_rules.append("cultural_adaptation")
        
        # Apply honorific respect
        if self.preferences['respect_honorifics']:
            modified_text = self._apply_honorific_respect(
                modified_text, source_lang, target_lang
            )
            applied_rules.append("honorific_respect")
        
        # Apply glossary
        if self.custom_glossary:
            modified_text = self._apply_glossary(modified_text)
            applied_rules.append("glossary_application")
        
        # Apply excluded terms
        if self.excluded_terms:
            modified_text = self._apply_excluded_terms(modified_text)
            applied_rules.append("excluded_terms")
        
        return modified_text, applied_rules
    
    def _apply_formal_style(self, text: str) -> str:
        """Apply formal style modifications."""
        formal_replacements = {
            'guys': 'individuals',
            'stuff': 'items',
            'get': 'obtain',
            'go': 'proceed',
            'like': 'similar to',
            'say': 'state',
        }
        
        for informal, formal in formal_replacements.items():
            text = text.replace(informal, formal)
        
        return text
    
    def _apply_casual_style(self, text: str) -> str:
        """Apply casual style modifications."""
        casual_replacements = {
            'individuals': 'guys',
            'items': 'stuff',
            'obtain': 'get',
            'proceed': 'go',
            'similar to': 'like',
            'state': 'say',
        }
        
        for formal, casual in casual_replacements.items():
            text = text.replace(formal, casual)
        
        return text
    
    def _apply_literary_tone(self, text: str) -> str:
        """Apply literary tone modifications."""
        return f"[Literary expression: {text}]"
    
    def _apply_conversational_tone(self, text: str) -> str:
        """Apply conversational tone modifications."""
        return f"[Conversational: {text}]"
    
    def _apply_cultural_adaptation(
        self,
        text: str,
        source_lang: str,
        target_lang: str
    ) -> str:
        """Apply cultural adaptation."""
        if source_lang == 'ja' and target_lang == 'en':
            # Adapt Japanese honorifics
            honorifics = ['san', 'kun', 'chan', 'sama', 'sensei']
            for honorific in honorifics:
                text = text.replace(honorific, '')
        
        return text
    
    def _apply_honorific_respect(
        self,
        text: str,
        source_lang: str,
        target_lang: str
    ) -> str:
        """Respect honorifics in translation."""
        if source_lang == 'ja' and target_lang == 'en':
            # Preserve context for honorifics
            text = f"(respecting honorifics: {text})"
        
        return text
    
    def _apply_glossary(self, text: str) -> str:
        """Apply custom glossary rules."""
        for source_term, details in self.custom_glossary.items():
            translation = details['translation']
            text = text.replace(source_term, translation)
        
        return text
    
    def _apply_excluded_terms(self, text: str) -> str:
        """Apply excluded terms rules."""
        for term in self.excluded_terms:
            text = text.replace(term, '')
        
        return text


class EnhancedTranslationResult:
    """
    Translation result with quality assessment and theme application.
    
    Extends the standard TranslationResult to include quality metrics
    and theme modifications.
    """
    
    def __init__(
        self,
        translated_text: str,
        model_used: str,
        input_tokens: int,
        output_tokens: int,
        estimated_cost: float,
        success: bool,
        error: Optional[str] = None,
        source_language: str = "zh",
        target_language: str = "en"
    ):
        self.translated_text = translated_text
        self.model_used = model_used
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.estimated_cost = estimated_cost
        self.success = success
        self.error = error
        
        # Quality assessment
        self.quality_indicator = TranslationQualityIndicator()
        self.quality_assessment = None
        self.quality_score = 0.0
        self.quality_level = None
        self.quality_issues = []
        self.quality_suggestions = []
        
        # Theme application
        self.applied_themes = []
        self.theme_modifications = []
        
        # Quality assessment if translation was successful
        if success and translated_text:
            self._perform_quality_assessment(source_language, target_language)
    
    def _perform_quality_assessment(self, source_lang: str, target_lang: str):
        """Perform quality assessment on the translation."""
        # Use empty original for quality assessment (in real usage, would need original)
        assessment = self.quality_indicator.assess_translation_quality(
            self.translated_text,
            "",  # Original text (simplified)
            source_lang,
            target_lang
        )
        
        self.quality_assessment = assessment
        self.quality_score = assessment.quality_score
        self.quality_level = assessment.quality_level
        self.quality_issues = [issue.value for issue in assessment.issues]
        self.quality_suggestions = assessment.suggestions
    
    def apply_theme(self, theme: TranslationTheme) -> 'EnhancedTranslationResult':
        """
        Apply a theme to this translation result.
        
        Returns:
            New EnhancedTranslationResult with theme applied
        """
        # Apply theme modifications
        modified_text, applied_rules = theme.apply_theme(
            self.translated_text,
            'zh',  # Assuming Chinese source
            'en',  # Assuming English target
            self.quality_indicator
        )
        
        # Create new result with modified text
        new_result = EnhancedTranslationResult(
            translated_text=modified_text,
            model_used=self.model_used,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            estimated_cost=self.estimated_cost,
            success=self.success,
            error=self.error,
            source_language='zh',
            target_language='en'
        )
        
        # Copy quality information
        new_result.quality_assessment = self.quality_assessment
        new_result.quality_score = self.quality_score
        new_result.quality_level = self.quality_level
        new_result.quality_issues = self.quality_issues
        new_result.quality_suggestions = self.quality_suggestions
        
        # Track applied themes
        new_result.applied_themes = self.applied_themes + [theme.name]
        new_result.theme_modifications.extend(applied_rules)
        
        return new_result
    
    def get_quality_report(self) -> Dict[str, Any]:
        """Get a comprehensive quality report."""
        return {
            'quality_level': self.quality_level.value if self.quality_level else None,
            'quality_score': self.quality_score,
            'issues': self.quality_issues,
            'suggestions': self.quality_suggestions,
            'needs_revision': self.quality_level == TranslationQuality.NEEDS_REVISION
        }


# Global instances
_quality_indicator = TranslationQualityIndicator()
_themes: Dict[str, TranslationTheme] = {}


def get_quality_indicator() -> TranslationQualityIndicator:
    """Get the global quality indicator instance."""
    return _quality_indicator


def get_theme(name: str) -> Optional[TranslationTheme]:
    """Get a theme by name."""
    return _themes.get(name)


def create_theme(name: str) -> TranslationTheme:
    """Create a new theme."""
    theme = TranslationTheme(name)
    _themes[name] = theme
    return theme


def get_all_themes() -> Dict[str, TranslationTheme]:
    """Get all available themes."""
    return _themes.copy()


def enhanced_translation_quality_wrapper():
    """
    Create a wrapper function for enhanced translation quality assessment.
    
    This can be used to monkey-patch existing translation methods
    or create new translation functions with quality assessment.
    """
    def quality_enhanced_translate(
        translated_text: str,
        model_used: str,
        input_tokens: int,
        output_tokens: int,
        estimated_cost: float,
        success: bool,
        error: Optional[str] = None,
        source_language: str = "zh",
        target_language: str = "en"
    ) -> EnhancedTranslationResult:
        """Enhanced translation with quality assessment."""
        return EnhancedTranslationResult(
            translated_text=translated_text,
            model_used=model_used,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost=estimated_cost,
            success=success,
            error=error,
            source_language=source_language,
            target_language=target_language
        )
    
    return quality_enhanced_translate


# Initialize default themes
create_theme("default")
create_theme("formal")
create_theme("casual")
create_theme("literary")

logger.info("Translation quality and theme system initialized successfully.")
