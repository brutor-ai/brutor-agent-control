# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Content Filter for all proxies

Comprehensive content filtering for proxy requests and responses:
- Input validation and sanitization
- Prompt injection detection
- PII (Personally Identifiable Information) detection and redaction
- Toxic content detection
- Custom content policies
- Output filtering
"""

import re
import hashlib
from typing import Dict, List, Optional, Any
from enum import Enum
from dataclasses import dataclass, field

from app.core.logging_system import BrutorLogger


class FilterAction(Enum):
    """Actions that can be taken when content violates policy"""
    ALLOW = "allow"  # Allow content through
    BLOCK = "block"  # Block the request entirely
    REDACT = "redact"  # Redact sensitive parts
    WARN = "warn"  # Allow but log warning
    MODERATE = "moderate"  # Queue for human review


class ViolationType(Enum):
    """Types of content policy violations"""
    PROMPT_INJECTION = "prompt_injection"
    PII = "pii"
    TOXIC_CONTENT = "toxic"
    JAILBREAK_ATTEMPT = "jailbreak"
    EXCESSIVE_TOKENS = "excessive_tokens"
    BANNED_CONTENT = "banned_content"
    CUSTOM_POLICY = "custom_policy"


@dataclass
class FilterResult:
    """Result of content filtering"""
    action: FilterAction
    violations: List[ViolationType]
    filtered_content: str
    original_content: str
    redacted_items: List[Dict[str, Any]]
    confidence: float  # 0.0 to 1.0
    details: Dict[str, Any]


@dataclass
class ContentPolicy:
    """Content filtering policy configuration"""
    # PII Detection
    detect_email: bool = True
    detect_phone: bool = True
    detect_ssn: bool = True
    detect_credit_card: bool = True
    detect_api_keys: bool = True

    # Prompt Injection Detection
    detect_prompt_injection: bool = True
    prompt_injection_sensitivity: float = 0.7  # 0.0 to 1.0

    # Toxic Content
    detect_toxic_content: bool = True
    toxic_threshold: float = 0.8  # 0.0 to 1.0

    # Jailbreak Detection
    detect_jailbreak: bool = True
    jailbreak_patterns: List[str] = field(default_factory=list)

    # Token Limits
    max_input_tokens: Optional[int] = None
    max_output_tokens: Optional[int] = None

    # Custom Rules
    banned_words: List[str] = field(default_factory=list)
    banned_patterns: List[str] = field(default_factory=list)
    allowed_domains: List[str] = field(default_factory=list)  # For URL filtering

    # Actions
    pii_action: FilterAction = FilterAction.REDACT
    injection_action: FilterAction = FilterAction.BLOCK
    toxic_action: FilterAction = FilterAction.WARN
    jailbreak_action: FilterAction = FilterAction.BLOCK


class ContentFilter:
    """
    Comprehensive content filter for LLM requests and responses

    Features:
    - PII detection and redaction
    - Prompt injection detection
    - Toxic content detection
    - Jailbreak attempt detection
    - Custom content policies
    - Multi-language support (extensible)
    """

    def __init__(self, policy: ContentPolicy, logger: Optional[BrutorLogger] = None):
        self.policy = policy
        self.logger = logger or BrutorLogger("content-filter")

        # Compile regex patterns for performance
        self._compile_patterns()

        # Known prompt injection patterns
        self._injection_patterns = self._load_injection_patterns()

        # Jailbreak patterns
        self._jailbreak_patterns = self._load_jailbreak_patterns()

    def _compile_patterns(self):
        """Compile regex patterns for efficient matching"""
        # Email pattern (RFC 5322 simplified)
        self.email_pattern = re.compile(
            r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
        )

        # Phone number patterns (US, international)
        self.phone_pattern = re.compile(
            r'(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}'
        )

        # SSN pattern (US Social Security Number)
        self.ssn_pattern = re.compile(
            r'\b\d{3}-\d{2}-\d{4}\b'
        )

        # Credit card pattern (basic)
        self.credit_card_pattern = re.compile(
            r'\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b'
        )

        # API key patterns (common formats)
        self.api_key_patterns = [
            re.compile(r'sk-[A-Za-z0-9]{48}'),  # OpenAI
            re.compile(r'sk-ant-[A-Za-z0-9\-]{95}'),  # Anthropic
            re.compile(r'[A-Za-z0-9_-]{40}'),  # Generic 40-char
        ]

        # IPv4 address
        self.ipv4_pattern = re.compile(
            r'\b(?:\d{1,3}\.){3}\d{1,3}\b'
        )

        # AWS Access Key
        self.aws_key_pattern = re.compile(
            r'AKIA[0-9A-Z]{16}'
        )

        # GitHub Token
        self.github_token_pattern = re.compile(
            r'ghp_[A-Za-z0-9]{36}'
        )

    def _load_injection_patterns(self) -> List[re.Pattern]:
        """Load known prompt injection patterns"""
        patterns = [
            # Direct instruction override
            r'ignore\s+(all\s+)?previous\s+instructions?',
            r'disregard\s+(all\s+)?prior\s+(instructions?|prompts?)',
            r'forget\s+(everything|all)\s+(you\s+)?(were\s+)?told',

            # Role manipulation
            r'you\s+are\s+now\s+a',
            r'act\s+as\s+(if\s+)?you\s+(are|were)',
            r'pretend\s+(to\s+be|you\s+are)',

            # System prompt leakage attempts
            r'show\s+me\s+your\s+(system\s+)?(prompt|instructions)',
            r'what\s+(are|were)\s+your\s+(original\s+)?(instructions|guidelines)',
            r'print\s+(your\s+)?(system\s+)?prompt',

            # Jailbreak phrases
            r'DAN\s+mode',
            r'developer\s+mode',
            r'sudo\s+mode',

            # Encoding tricks
            r'base64|rot13|decode',

            # Bypass attempts
            r'bypass\s+(your\s+)?(filters?|restrictions?|guidelines?)',
            r'ignore\s+(your\s+)?(safety|ethical)\s+guidelines?',
        ]

        return [re.compile(p, re.IGNORECASE) for p in patterns]

    def _load_jailbreak_patterns(self) -> List[re.Pattern]:
        """Load jailbreak attempt patterns"""
        patterns = [
            # DAN (Do Anything Now) variants
            r'DAN\s*\d*\s*mode',
            r'do\s+anything\s+now',

            # Character roleplay jailbreaks
            r'you\s+are\s+an?\s+unrestricted\s+AI',
            r'with(out)?\s+any\s+(moral|ethical)\s+constraints?',

            # Hypothetical scenarios
            r'in\s+a\s+hypothetical\s+(world|scenario)',
            r'imagine\s+(a\s+)?world\s+where',

            # Translation tricks
            r'translate\s+this\s+to',
            r'write\s+in\s+(spanish|french|german|chinese)',
        ]

        # Add custom jailbreak patterns from policy
        patterns.extend(self.policy.jailbreak_patterns)

        return [re.compile(p, re.IGNORECASE) for p in patterns]

    # =========================================================================
    # Main Filtering Methods
    # =========================================================================

    def filter_input(self, content: str, metadata: Optional[Dict[str, Any]] = None) -> FilterResult:
        """
        Filter input content (user prompts) before sending to LLM

        Args:
            content: Input text to filter
            metadata: Additional context (user_id, session_id, etc.)

        Returns:
            FilterResult with action and filtered content
        """
        violations = []
        filtered_content = content
        redacted_items = []
        details = {}

        # 1. Detect PII
        if self.policy.detect_email or self.policy.detect_phone or self.policy.detect_ssn:
            pii_result = self._detect_pii(filtered_content)
            if pii_result['found']:
                violations.append(ViolationType.PII)
                details['pii'] = pii_result

                if self.policy.pii_action == FilterAction.REDACT:
                    filtered_content = pii_result['redacted_text']
                    redacted_items.extend(pii_result['items'])

        # 2. Detect Prompt Injection
        if self.policy.detect_prompt_injection:
            injection_result = self._detect_prompt_injection(content)
            if injection_result['detected']:
                violations.append(ViolationType.PROMPT_INJECTION)
                details['prompt_injection'] = injection_result

                if self.policy.injection_action == FilterAction.BLOCK:
                    self.logger.warning("Prompt injection detected", context={
                        "metadata": metadata,
                        "patterns_matched": injection_result['patterns_matched'],
                        "confidence": injection_result['confidence']
                    })
                    return FilterResult(
                        action=FilterAction.BLOCK,
                        violations=violations,
                        filtered_content="",
                        original_content=content,
                        redacted_items=[],
                        confidence=injection_result['confidence'],
                        details=details
                    )

        # 3. Detect Jailbreak Attempts
        if self.policy.detect_jailbreak:
            jailbreak_result = self._detect_jailbreak(content)
            if jailbreak_result['detected']:
                violations.append(ViolationType.JAILBREAK_ATTEMPT)
                details['jailbreak'] = jailbreak_result

                if self.policy.jailbreak_action == FilterAction.BLOCK:
                    self.logger.warning("Jailbreak attempt detected", context={
                        "metadata": metadata,
                        "patterns_matched": jailbreak_result['patterns_matched']
                    })
                    return FilterResult(
                        action=FilterAction.BLOCK,
                        violations=violations,
                        filtered_content="",
                        original_content=content,
                        redacted_items=[],
                        confidence=jailbreak_result['confidence'],
                        details=details
                    )

        # 4. Check Banned Words/Patterns
        banned_result = self._check_banned_content(filtered_content)
        if banned_result['found']:
            violations.append(ViolationType.BANNED_CONTENT)
            details['banned_content'] = banned_result
            filtered_content = banned_result['filtered_text']

        # 5. Token Limit Check
        if self.policy.max_input_tokens:
            token_count = self._estimate_tokens(filtered_content)
            if token_count > self.policy.max_input_tokens:
                violations.append(ViolationType.EXCESSIVE_TOKENS)
                details['token_count'] = token_count
                details['max_tokens'] = self.policy.max_input_tokens

        # Determine final action — most restrictive wins
        action = FilterAction.ALLOW
        confidence = 0.0

        if violations:
            _action_priority = {
                FilterAction.ALLOW: 0,
                FilterAction.WARN: 1,
                FilterAction.MODERATE: 1,
                FilterAction.REDACT: 2,
                FilterAction.BLOCK: 3,
            }

            candidate_actions: List[FilterAction] = []

            if ViolationType.PROMPT_INJECTION in violations:
                candidate_actions.append(self.policy.injection_action)
                confidence = max(confidence, details.get('prompt_injection', {}).get('confidence', 0.0))
            if ViolationType.JAILBREAK_ATTEMPT in violations:
                candidate_actions.append(self.policy.jailbreak_action)
                confidence = max(confidence, details.get('jailbreak', {}).get('confidence', 0.0))
            if ViolationType.PII in violations:
                candidate_actions.append(self.policy.pii_action)
                confidence = max(confidence, 0.9)
            if ViolationType.TOXIC_CONTENT in violations:
                candidate_actions.append(self.policy.toxic_action)
                confidence = max(confidence, details.get('toxic_content', {}).get('confidence', 0.0))
            if ViolationType.BANNED_CONTENT in violations:
                candidate_actions.append(FilterAction.BLOCK)
                confidence = max(confidence, 0.95)
            if ViolationType.EXCESSIVE_TOKENS in violations:
                candidate_actions.append(FilterAction.BLOCK)
                confidence = max(confidence, 1.0)

            if candidate_actions:
                action = max(candidate_actions, key=lambda a: _action_priority.get(a, 0))
            else:
                confidence = 0.5

        return FilterResult(
            action=action,
            violations=violations,
            filtered_content=filtered_content,
            original_content=content,
            redacted_items=redacted_items,
            confidence=confidence,
            details=details
        )

    def filter_output(self, content: str, metadata: Optional[Dict[str, Any]] = None) -> FilterResult:
        """
        Filter output content (LLM responses) before returning to user

        Args:
            content: Output text to filter
            metadata: Additional context

        Returns:
            FilterResult with action and filtered content
        """
        violations = []
        filtered_content = content
        redacted_items = []
        details = {}

        # 1. Detect PII in output (model might generate PII)
        if self.policy.detect_email or self.policy.detect_phone:
            pii_result = self._detect_pii(filtered_content)
            if pii_result['found']:
                violations.append(ViolationType.PII)
                details['pii'] = pii_result

                if self.policy.pii_action == FilterAction.REDACT:
                    filtered_content = pii_result['redacted_text']
                    redacted_items.extend(pii_result['items'])

        # 2. Detect Toxic Content
        if self.policy.detect_toxic_content:
            toxic_result = self._detect_toxic_content(filtered_content)
            if toxic_result['detected']:
                violations.append(ViolationType.TOXIC_CONTENT)
                details['toxic_content'] = toxic_result

                if self.policy.toxic_action == FilterAction.BLOCK:
                    self.logger.warning("Toxic content in model output", context={
                        "metadata": metadata,
                        "confidence": toxic_result['confidence']
                    })
                    return FilterResult(
                        action=FilterAction.BLOCK,
                        violations=violations,
                        filtered_content="[Content filtered due to policy violation]",
                        original_content=content,
                        redacted_items=[],
                        confidence=toxic_result['confidence'],
                        details=details
                    )

        # 3. Check for system prompt leakage
        system_leak = self._detect_system_prompt_leak(filtered_content)
        if system_leak['detected']:
            violations.append(ViolationType.CUSTOM_POLICY)
            details['system_leak'] = system_leak
            filtered_content = system_leak['filtered_text']

        # Determine final action — most restrictive wins
        action = FilterAction.ALLOW
        confidence = 0.0

        if violations:
            _action_priority = {
                FilterAction.ALLOW: 0,
                FilterAction.WARN: 1,
                FilterAction.MODERATE: 1,
                FilterAction.REDACT: 2,
                FilterAction.BLOCK: 3,
            }

            candidate_actions: List[FilterAction] = []

            if ViolationType.PII in violations:
                candidate_actions.append(self.policy.pii_action)
                confidence = max(confidence, details.get('pii', {}).get('confidence', 0.9))
            if ViolationType.TOXIC_CONTENT in violations:
                candidate_actions.append(self.policy.toxic_action)
                confidence = max(confidence, details.get('toxic_content', {}).get('confidence', 0.0))
            if ViolationType.CUSTOM_POLICY in violations:
                candidate_actions.append(FilterAction.REDACT)
                confidence = max(confidence, details.get('system_leak', {}).get('confidence', 0.0))

            if candidate_actions:
                action = max(candidate_actions, key=lambda a: _action_priority.get(a, 0))
            else:
                action = FilterAction.WARN
                confidence = max(confidence, 0.5)

        return FilterResult(
            action=action,
            violations=violations,
            filtered_content=filtered_content,
            original_content=content,
            redacted_items=redacted_items,
            confidence=confidence,
            details=details
        )

    # =========================================================================
    # Detection Methods
    # =========================================================================

    def _detect_pii(self, text: str) -> Dict[str, Any]:
        """Detect and redact PII (Personally Identifiable Information)"""
        found = False
        redacted_text = text
        items = []

        # Email addresses
        if self.policy.detect_email:
            for match in self.email_pattern.finditer(text):
                found = True
                email = match.group(0)
                redacted = f"[EMAIL_{hashlib.md5(email.encode()).hexdigest()[:8]}]"
                redacted_text = redacted_text.replace(email, redacted)
                items.append({
                    'type': 'email',
                    'original': email,
                    'redacted': redacted,
                    'position': match.span()
                })

        # Phone numbers
        if self.policy.detect_phone:
            for match in self.phone_pattern.finditer(text):
                found = True
                phone = match.group(0)
                redacted = f"[PHONE_{hashlib.md5(phone.encode()).hexdigest()[:8]}]"
                redacted_text = redacted_text.replace(phone, redacted)
                items.append({
                    'type': 'phone',
                    'original': phone,
                    'redacted': redacted,
                    'position': match.span()
                })

        # SSN
        if self.policy.detect_ssn:
            for match in self.ssn_pattern.finditer(text):
                found = True
                ssn = match.group(0)
                redacted = "[SSN_REDACTED]"
                redacted_text = redacted_text.replace(ssn, redacted)
                items.append({
                    'type': 'ssn',
                    'original': 'XXX-XX-' + ssn[-4:],  # Keep last 4 digits
                    'redacted': redacted,
                    'position': match.span()
                })

        # Credit cards
        if self.policy.detect_credit_card:
            for match in self.credit_card_pattern.finditer(text):
                found = True
                cc = match.group(0)
                # Luhn algorithm check (basic validation)
                if self._validate_credit_card(cc.replace('-', '').replace(' ', '')):
                    redacted = "[CC_REDACTED]"
                    redacted_text = redacted_text.replace(cc, redacted)
                    items.append({
                        'type': 'credit_card',
                        'original': '*' * (len(cc) - 4) + cc[-4:],
                        'redacted': redacted,
                        'position': match.span()
                    })

        # API Keys
        if self.policy.detect_api_keys:
            for pattern in self.api_key_patterns:
                for match in pattern.finditer(text):
                    found = True
                    key = match.group(0)
                    redacted = "[API_KEY_REDACTED]"
                    redacted_text = redacted_text.replace(key, redacted)
                    items.append({
                        'type': 'api_key',
                        'original': key[:8] + '...',
                        'redacted': redacted,
                        'position': match.span()
                    })

            # AWS Keys
            for match in self.aws_key_pattern.finditer(text):
                found = True
                key = match.group(0)
                redacted = "[AWS_KEY_REDACTED]"
                redacted_text = redacted_text.replace(key, redacted)
                items.append({
                    'type': 'aws_key',
                    'original': key[:8] + '...',
                    'redacted': redacted,
                    'position': match.span()
                })

            # GitHub Tokens
            for match in self.github_token_pattern.finditer(text):
                found = True
                token = match.group(0)
                redacted = "[GITHUB_TOKEN_REDACTED]"
                redacted_text = redacted_text.replace(token, redacted)
                items.append({
                    'type': 'github_token',
                    'original': token[:8] + '...',
                    'redacted': redacted,
                    'position': match.span()
                })

        return {
            'found': found,
            'redacted_text': redacted_text,
            'items': items,
            'count': len(items)
        }

    def _detect_prompt_injection(self, text: str) -> Dict[str, Any]:
        """Detect prompt injection attempts"""
        detected = False
        patterns_matched = []
        confidence = 0.0

        text_lower = text.lower()

        # Check against known patterns
        for i, pattern in enumerate(self._injection_patterns):
            if pattern.search(text_lower):
                detected = True
                patterns_matched.append({
                    'pattern_id': i,
                    'match': pattern.pattern
                })

        # Calculate confidence based on number of patterns matched
        if patterns_matched:
            confidence = min(1.0, len(patterns_matched) * 0.3)

        # Check for suspicious patterns
        suspicious_indicators = [
            'ignore',
            'disregard',
            'forget',
            'system prompt',
            'instructions',
            'override',
        ]

        indicator_count = sum(1 for ind in suspicious_indicators if ind in text_lower)
        if indicator_count >= 3:
            detected = True
            confidence = max(confidence, 0.6)

        return {
            'detected': detected,
            'confidence': confidence,
            'patterns_matched': patterns_matched,
            'suspicious_indicator_count': indicator_count
        }

    def _detect_jailbreak(self, text: str) -> Dict[str, Any]:
        """Detect jailbreak attempts"""
        detected = False
        patterns_matched = []
        confidence = 0.0

        text_lower = text.lower()

        for i, pattern in enumerate(self._jailbreak_patterns):
            if pattern.search(text_lower):
                detected = True
                patterns_matched.append({
                    'pattern_id': i,
                    'match': pattern.pattern
                })

        if patterns_matched:
            confidence = min(1.0, len(patterns_matched) * 0.4)

        return {
            'detected': detected,
            'confidence': confidence,
            'patterns_matched': patterns_matched
        }

    def _detect_toxic_content(self, text: str) -> Dict[str, Any]:
        """
        Detect toxic content

        Note: This is a basic implementation. In production, you would
        integrate with a proper toxicity detection API like:
        - Perspective API (Google)
        - Azure Content Moderator
        - OpenAI Moderation API
        """
        # Built-in keyword fallback used when no toxicity provider
        # is configured on the guardrail. Production toxic-content
        # detection is provided by the standalone Rust providers
        # (BrutorGuard, Bedrock Guardrails, Presidio) selected via
        # `toxic_content.provider`; the Rust orchestrator skips this
        # path entirely when one of those is set. Keeping this
        # keyword list small + literal on purpose — operators who
        # need real ML toxicity should configure a provider rather
        # than expanding this list.
        toxic_keywords = ['hate', 'violence', 'threat']

        text_lower = text.lower()
        detected_keywords = [kw for kw in toxic_keywords if kw in text_lower]

        detected = len(detected_keywords) > 0
        confidence = min(1.0, len(detected_keywords) * 0.3) if detected else 0.0

        return {
            'detected': detected,
            'confidence': confidence,
            'keywords_detected': detected_keywords,
            'note': 'Built-in keyword fallback. Configure a toxicity provider for ML-based detection.',
        }

    def _detect_system_prompt_leak(self, text: str) -> Dict[str, Any]:
        """Detect if LLM output contains system prompt leakage"""
        detected = False
        filtered_text = text

        # Patterns that suggest system prompt leakage
        leak_patterns = [
            r'(you are|i am) an? (AI )?assistant (created|developed|made) by',
            r'my (system )?instructions? (are|were|include)',
            r'(according to|based on) my (system )?prompt',
        ]

        for pattern in leak_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                detected = True
                # Optionally redact the leaked portion
                filtered_text = re.sub(pattern, '[REDACTED]', text, flags=re.IGNORECASE)

        return {
            'detected': detected,
            'filtered_text': filtered_text,
            'confidence': 0.8 if detected else 0.0
        }

    def _check_banned_content(self, text: str) -> Dict[str, Any]:
        """Check for banned words and patterns"""
        found = False
        filtered_text = text
        matched_items = []

        text_lower = text.lower()

        # Check banned words
        for word in self.policy.banned_words:
            if word.lower() in text_lower:
                found = True
                matched_items.append({'type': 'word', 'value': word})
                filtered_text = re.sub(
                    r'\b' + re.escape(word) + r'\b',
                    '[REDACTED]',
                    filtered_text,
                    flags=re.IGNORECASE
                )

        # Check banned patterns
        for pattern_str in self.policy.banned_patterns:
            pattern = re.compile(pattern_str, re.IGNORECASE)
            if pattern.search(text):
                found = True
                matched_items.append({'type': 'pattern', 'value': pattern_str})
                filtered_text = pattern.sub('[REDACTED]', filtered_text)

        return {
            'found': found,
            'filtered_text': filtered_text,
            'matched_items': matched_items
        }

    # =========================================================================
    # Utility Methods
    # =========================================================================

    def _validate_credit_card(self, number: str) -> bool:
        """Validate credit card number using Luhn algorithm"""
        try:
            digits = [int(d) for d in number if d.isdigit()]
            if len(digits) < 13 or len(digits) > 19:
                return False

            checksum = 0
            for i, digit in enumerate(reversed(digits)):
                if i % 2 == 1:
                    digit *= 2
                    if digit > 9:
                        digit -= 9
                checksum += digit

            return checksum % 10 == 0
        except:
            return False

    def _estimate_tokens(self, text: str) -> int:
        """
        Estimate token count

        Note: This is a rough approximation. For accurate counts, use:
        - tiktoken for OpenAI models
        - anthropic tokenizer for Claude
        - Model-specific tokenizers
        """
        # Rough estimate: ~4 characters per token for English text
        return len(text) // 4

    def update_policy(self, policy: ContentPolicy) -> None:
        """Update the content filtering policy"""
        self.policy = policy
        self._compile_patterns()
        self._jailbreak_patterns = self._load_jailbreak_patterns()

        self.logger.info("Content filter policy updated", context={
            "detect_pii": policy.detect_email or policy.detect_phone or policy.detect_ssn,
            "detect_injection": policy.detect_prompt_injection,
            "detect_jailbreak": policy.detect_jailbreak,
            "detect_toxic": policy.detect_toxic_content
        })

    def get_statistics(self) -> Dict[str, Any]:
        """Get filter statistics (extend as needed)"""
        return {
            "policy": {
                "pii_detection": self.policy.detect_email or self.policy.detect_phone,
                "injection_detection": self.policy.detect_prompt_injection,
                "jailbreak_detection": self.policy.detect_jailbreak,
                "toxic_detection": self.policy.detect_toxic_content,
            },
            "patterns_loaded": {
                "injection_patterns": len(self._injection_patterns),
                "jailbreak_patterns": len(self._jailbreak_patterns),
                "banned_words": len(self.policy.banned_words),
                "banned_patterns": len(self.policy.banned_patterns),
            }
        }


# =============================================================================
# Filter Result Helpers
# =============================================================================

def should_block_request(filter_result: FilterResult) -> bool:
    """Helper to determine if request should be blocked"""
    return filter_result.action == FilterAction.BLOCK


def should_redact_content(filter_result: FilterResult) -> bool:
    """Helper to determine if content should be redacted"""
    return filter_result.action == FilterAction.REDACT


def get_filtered_content(filter_result: FilterResult) -> str:
    """Helper to get filtered/redacted content"""
    return filter_result.filtered_content if should_redact_content(filter_result) else filter_result.original_content


def format_block_message(filter_result: FilterResult) -> str:
    """Format user-friendly message when content is blocked"""
    violation_names = [v.value.replace('_', ' ').title() for v in filter_result.violations]

    if not violation_names:
        return "Your request cannot be processed due to content policy violations."

    violations_str = ", ".join(violation_names)
    return f"Your request cannot be processed due to: {violations_str}. Please review our content policy and try again."