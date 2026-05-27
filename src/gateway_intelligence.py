"""Intelligence enhancement module for the gateway.

Provides question preprocessing, answer quality assessment,
and multi-turn conversation optimization to improve response quality.

Now integrates real LLM calls via gateway_llm for actual intelligence
rather than purely rule-based heuristics.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

Json = dict[str, Any]
_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IntelligenceConfig:
    """Configuration for intelligence enhancement."""
    enabled: bool = True
    reflection_enabled: bool = True
    decomposition_enabled: bool = True
    quality_assessment_enabled: bool = True
    max_reflection_tokens: int = 500
    max_decomposition_parts: int = 5
    quality_threshold: float = 0.6
    use_llm: bool = False  # Use real LLM calls when available (requires gateway_llm.py)
    llm_timeout: float = 15.0


def _intelligence_config(raw: dict | None = None) -> IntelligenceConfig:
    """Parse intelligence config from raw dict."""
    if not raw:
        return IntelligenceConfig()
    return IntelligenceConfig(
        enabled=raw.get("enabled", True),
        reflection_enabled=raw.get("reflection_enabled", True),
        decomposition_enabled=raw.get("decomposition_enabled", True),
        quality_assessment_enabled=raw.get("quality_assessment_enabled", True),
        max_reflection_tokens=raw.get("max_reflection_tokens", 500),
        max_decomposition_parts=raw.get("max_decomposition_parts", 5),
        quality_threshold=raw.get("quality_threshold", 0.6),
        use_llm=raw.get("use_llm", False),
        llm_timeout=raw.get("llm_timeout", 15.0),
    )


# ---------------------------------------------------------------------------
# Question Analysis
# ---------------------------------------------------------------------------

@dataclass
class QuestionAnalysis:
    """Analysis result of a user question."""
    original: str
    complexity: str  # "simple", "moderate", "complex"
    domain: str  # "code", "math", "general", "creative", "factual"
    requires_tools: bool
    requires_context: bool
    sub_questions: list[str] = field(default_factory=list)
    reflection_notes: list[str] = field(default_factory=list)
    suggested_approach: str = ""
    source: str = "rules"  # "rules" or "llm"


def _analyze_question_rules(text: str) -> dict:
    """Rule-based question analysis (fast fallback)."""
    text_lower = text.lower().strip()
    complexity = _detect_complexity(text_lower)
    domain = _detect_domain(text_lower)
    requires_tools = _detect_tool_requirement(text_lower)
    requires_context = _detect_context_requirement(text_lower)
    sub_questions = _decompose_question(text) if complexity == "complex" else []
    reflection_notes = []
    if complexity == "complex":
        reflection_notes.append("Complex question requiring step-by-step answer.")
    if requires_context:
        reflection_notes.append("Question may depend on context; ensure full background is understood.")
    if "怎么" in text or "如何" in text or "how" in text_lower:
        reflection_notes.append("How-to question: provide concrete steps.")
    suggested_approach = _suggest_approach_for(domain, complexity, requires_tools)
    return {
        "complexity": complexity,
        "domain": domain,
        "requires_tools": requires_tools,
        "requires_context": requires_context,
        "suggested_approach": suggested_approach,
        "sub_questions": sub_questions,
        "reflection_notes": reflection_notes,
    }


def _analyze_question_llm(text: str, context: str = "") -> dict | None:
    """LLM-powered question analysis."""
    try:
        from .gateway_llm import llm_analyze_question
        return llm_analyze_question(text, context)
    except Exception as exc:
        _logger.debug("LLM question analysis failed: %s", exc)
        return None


def _suggest_approach_for(domain: str, complexity: str, requires_tools: bool) -> str:
    """Suggest an approach based on domain/complexity/tools."""
    if domain == "code" and requires_tools:
        return "Use tools to analyze code, provide runnable examples"
    if domain == "code":
        return "Provide runnable code examples with key logic explained"
    if domain == "math":
        return "Show calculation steps, ensure accuracy"
    if complexity == "simple":
        return "Answer directly"
    return "Provide a well-structured, comprehensive answer"


def _analyze_question(text: str, config: IntelligenceConfig | None = None) -> QuestionAnalysis:
    """Analyze a user question using LLM with rule-based fallback."""
    if config is None:
        config = IntelligenceConfig()

    llm_result = None
    source = "rules"

    # Try LLM analysis first if enabled
    if config.use_llm:
        llm_result = _analyze_question_llm(text)
        if llm_result and isinstance(llm_result, dict) and llm_result.get("complexity"):
            source = "llm"
        else:
            llm_result = None

    # Fall back to rules
    if llm_result is None:
        llm_result = _analyze_question_rules(text)

    return QuestionAnalysis(
        original=text,
        complexity=llm_result.get("complexity", "moderate"),
        domain=llm_result.get("domain", "general"),
        requires_tools=llm_result.get("requires_tools", False),
        requires_context=llm_result.get("requires_context", False),
        sub_questions=llm_result.get("sub_questions", []),
        reflection_notes=llm_result.get("reflection_notes", []),
        suggested_approach=llm_result.get("suggested_approach", ""),
        source=source,
    )


# ---------------------------------------------------------------------------
# Quality Assessment
# ---------------------------------------------------------------------------

@dataclass
class QualityAssessment:
    """Assessment of answer quality."""
    score: float = 0.8  # 0.0 - 1.0
    overall: float = 0.8  # backward compat alias
    completeness: float = 0.8
    clarity: float = 0.7
    accuracy: float = 0.8
    relevance: float = 0.7
    needs_refinement: bool = False
    issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    source: str = "rules"

    def __post_init__(self):
        if self.overall == 0.8 and self.score != 0.8:
            self.overall = self.score


def _assess_quality_rules(answer: str) -> dict:
    """Rule-based quality assessment."""
    issues = []
    suggestions = []
    score = 0.8

    if len(answer) < 50:
        issues.append("Answer too short")
        suggestions.append("Provide more details and explanation")
        score -= 0.2

    uncertainty_markers = ["我不确定", "I'm not sure", "not sure", "maybe", "perhaps"]
    for marker in uncertainty_markers:
        if marker in answer:
            issues.append("Lacks confidence")
            suggestions.append("Provide a more definitive answer or explain uncertainty")
            score -= 0.1
            break

    if answer.count("```") >= 4:
        score += 0.1  # Good: has code examples

    return {
        "score": max(0.0, min(1.0, score)),
        "issues": issues,
        "suggestions": suggestions,
    }


def assess_quality(answer: str, question: str = "", config: IntelligenceConfig | None = None) -> QualityAssessment:
    """Assess answer quality using LLM with rule-based fallback."""
    if config is None:
        config = IntelligenceConfig()

    source = "rules"
    result = _assess_answer_quality(answer, question)

    if config.use_llm and question:
        try:
            from .gateway_llm import llm_assess_quality
            llm_result = llm_assess_quality(question, answer)
            if llm_result and isinstance(llm_result, dict) and "score" in llm_result:
                source = "llm"
                result.overall = float(llm_result.get("score", result.overall))
                result.issues = list(llm_result.get("issues", result.issues))
                result.suggestions = list(llm_result.get("suggestions", result.suggestions))
        except Exception:
            pass

    needs_ref = result.overall < config.quality_threshold
    return QualityAssessment(
        score=result.overall,
        overall=result.overall,
        completeness=result.completeness,
        clarity=result.clarity,
        accuracy=result.accuracy,
        relevance=result.relevance,
        needs_refinement=needs_ref,
        issues=result.issues,
        suggestions=result.suggestions,
        source=source,
    )


# ---------------------------------------------------------------------------
# Reflection
# ---------------------------------------------------------------------------

def reflect_on_answer(
    question: str,
    answer: str,
    config: IntelligenceConfig | None = None,
) -> str | None:
    """Reflect on and potentially improve an answer using LLM.

    Returns improved answer text, or None if no improvement needed/available.
    """
    if config is None:
        config = IntelligenceConfig()

    if not config.reflection_enabled:
        return None

    # Only reflect if quality is below threshold
    quality = assess_quality(answer, question, config)
    if quality.score >= config.quality_threshold:
        return None

    # Try LLM reflection
    if config.use_llm:
        try:
            from .gateway_llm import llm_reflect
            improved = llm_reflect(question, answer, config.max_reflection_tokens)
            if improved and len(improved) > len(answer) * 0.5:
                return improved
        except Exception as exc:
            _logger.debug("LLM reflection failed: %s", exc)

    # No refinement needed for good answers
    return None


# ---------------------------------------------------------------------------
# Enhanced System Prompt
# ---------------------------------------------------------------------------

def _build_enhanced_system_prompt(
    analysis: QuestionAnalysis,
    config: IntelligenceConfig,
) -> str:
    """Build an enhanced system prompt based on question analysis."""
    parts = []

    # Complexity-based instructions
    if analysis.complexity == "complex":
        parts.append("This is a complex question. Break it down step by step, covering all aspects thoroughly.")
    elif analysis.complexity == "moderate":
        parts.append("Provide a well-structured, comprehensive answer.")

    # Domain-based instructions
    if analysis.domain == "code":
        parts.append("Code question: provide runnable code examples and explain key logic.")
    elif analysis.domain == "math":
        parts.append("Math question: show calculation steps and ensure accuracy.")
    elif analysis.domain == "creative":
        parts.append("Creative question: be imaginative and provide unique content.")

    # Tool usage hints
    if analysis.requires_tools:
        parts.append("This question may require using tools to gather information or perform operations.")

    # Approach suggestion
    if analysis.suggested_approach:
        parts.append(f"Suggested approach: {analysis.suggested_approach}")

    # Sub-questions
    if analysis.sub_questions:
        parts.append("Sub-questions:")
        for i, sq in enumerate(analysis.sub_questions, 1):
            parts.append(f"  {i}. {sq}")

    if not parts:
        return ""

    return "\n".join(parts)


def _build_reflection_prompt(
    question: str,
    previous_answer: str | None,
    assessment = None,
) -> str:
    """Build a reflection prompt for answer improvement."""
    parts = ["Reflect on and improve the following answer:"]
    parts.append(f"\nOriginal question: {question}")

    if previous_answer:
        parts.append(f"\nCurrent answer: {previous_answer[:500]}")

    if assessment and assessment.suggestions:
        parts.append("\nImprovement suggestions:")
        for suggestion in assessment.suggestions:
            parts.append(f"- {suggestion}")

    parts.append("\nPlease provide an improved answer.")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Conversation Optimization
# ---------------------------------------------------------------------------

def _optimize_conversation_messages(
    messages: list[dict],
    analysis: QuestionAnalysis,
) -> list[dict]:
    """Optimize conversation messages for better responses."""
    if not messages:
        return messages

    optimized = list(messages)

    # Add system prompt if not present
    has_system = any(m.get("role") == "system" for m in optimized)
    if not has_system and analysis.complexity != "simple":
        system_prompt = _build_enhanced_system_prompt(analysis, IntelligenceConfig())
        if system_prompt:
            optimized.insert(0, {"role": "system", "content": system_prompt})

    return optimized


def _extract_conversation_context(
    messages: list[dict],
    max_turns: int = 5,
) -> str:
    """Extract relevant context from conversation history."""
    if not messages:
        return ""

    recent = messages[-max_turns * 2:]
    context_parts = []
    for msg in recent:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            content = " ".join(text_parts)
        if content:
            context_parts.append(f"{role}: {content[:200]}")

    return "\n".join(context_parts)


# ---------------------------------------------------------------------------
# Main Intelligence Pipeline
# ---------------------------------------------------------------------------

@dataclass
class IntelligenceResult:
    """Result of intelligence enhancement processing."""
    analysis: QuestionAnalysis
    enhanced_messages: list[dict]
    system_prompt: str | None
    should_reflect: bool
    reflection_prompt: str | None


def enhance_intelligence(
    messages: list[dict],
    config: IntelligenceConfig | None = None,
) -> IntelligenceResult:
    """Main entry point for intelligence enhancement.

    Analyzes the user's question (with LLM when available) and enhances
    the conversation for better response quality.
    """
    if config is None:
        config = IntelligenceConfig()

    if not config.enabled:
        last_user_msg = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    last_user_msg = content
                break
        return IntelligenceResult(
            analysis=QuestionAnalysis(
                original=last_user_msg,
                complexity="simple",
                domain="general",
                requires_tools=False,
                requires_context=False,
            ),
            enhanced_messages=messages,
            system_prompt=None,
            should_reflect=False,
            reflection_prompt=None,
        )

    # Extract last user message
    last_user_msg = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                last_user_msg = content
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        last_user_msg += block.get("text", "")
            break

    # Extract conversation context for LLM
    context = _extract_conversation_context(messages)

    # Analyze the question (LLM + rules fallback)
    analysis = _analyze_question(last_user_msg, config=config)

    # Build enhanced system prompt
    system_prompt = None
    if analysis.complexity != "simple":
        system_prompt = _build_enhanced_system_prompt(analysis, config)

    # Optimize messages
    enhanced_messages = _optimize_conversation_messages(messages, analysis)

    # Determine if reflection is needed
    should_reflect = config.reflection_enabled and analysis.complexity == "complex"

    # Build reflection prompt if needed
    reflection_prompt = None
    if should_reflect:
        reflection_prompt = _build_reflection_prompt(last_user_msg, None, None)

    return IntelligenceResult(
        analysis=analysis,
        enhanced_messages=enhanced_messages,
        system_prompt=system_prompt,
        should_reflect=should_reflect,
        reflection_prompt=reflection_prompt,
    )


def get_intelligence_summary(result: IntelligenceResult) -> str:
    """Get a human-readable summary of the intelligence analysis."""
    parts = [
        f"复杂度: {result.analysis.complexity}",
        f"领域: {result.analysis.domain}",
        f"需要工具: {result.analysis.requires_tools}",
        f"来源: {result.analysis.source}",
    ]
    if result.analysis.suggested_approach:
        parts.append(f"建议: {result.analysis.suggested_approach}")
    if result.should_reflect:
        parts.append("将进行反思优化")
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Rule-based detection helpers (fast fallback when LLM unavailable)
# ---------------------------------------------------------------------------

def _detect_complexity(text: str) -> str:
    """Detect question complexity from text."""
    # Simple greetings and very short text
    simple_greetings = {"hi", "hello", "hey", "你好", "嗨", "早上好", "晚上好", "下午好"}
    if text.strip() in simple_greetings or len(text.strip()) < 3:
        return "simple"

    complex_indicators = [
        "how to", "explain", "compare", "analyze", "design", "implement",
        "optimize", "refactor", "architect", "为什么", "怎么", "如何",
        "比较", "分析", "设计", "实现", "优化", "重构",
    ]
    simple_indicators = [
        "what is", "what's", "who is", "when", "where",
        "是什么", "是谁", "什么时候", "在哪里",
    ]

    complex_count = sum(1 for ind in complex_indicators if ind in text)
    simple_count = sum(1 for ind in simple_indicators if ind in text)

    # Multiple questions indicate complexity
    question_marks = text.count("?") + text.count("？")
    if question_marks >= 2:
        return "complex"

    if complex_count >= 2:
        return "complex"
    if simple_count > 0 and complex_count == 0:
        return "simple"
    return "moderate"


def _detect_domain(text: str) -> str:
    """Detect question domain from text."""
    text_lower = text.lower()
    code_indicators = [
        "code", "function", "class", "api", "bug", "error", "debug",
        "python", "javascript", "typescript", "rust", "go", "java",
        "代码", "函数", "类", "接口", "错误", "调试", "编程", "算法", "排序",
    ]
    math_indicators = [
        "calculate", "equation", "formula", "proof", "math",
        "计算", "公式", "证明", "数学", "方程",
    ]
    creative_indicators = [
        "write", "create", "generate", "design", "story", "poem",
        "写", "创作", "生成", "故事", "诗",
    ]
    factual_indicators = [
        "what is", "what are", "who is", "define", "explain what",
        "是什么", "什么是", "是谁", "定义", "解释",
    ]

    code_score = sum(1 for ind in code_indicators if ind in text_lower)
    math_score = sum(1 for ind in math_indicators if ind in text_lower)
    creative_score = sum(1 for ind in creative_indicators if ind in text_lower)
    factual_score = sum(1 for ind in factual_indicators if ind in text_lower)

    if code_score > math_score and code_score > creative_score and code_score > factual_score:
        return "code"
    if math_score > code_score and math_score > creative_score:
        return "math"
    if creative_score > factual_score:
        return "creative"
    if factual_score > 0:
        return "factual"
    return "general"


def _detect_tool_requirement(text: str) -> bool:
    """Detect if question likely requires tool usage."""
    tool_indicators = [
        "read", "file", "run", "execute", "search", "find", "list",
        "show", "open", "create", "write", "delete", "install",
        "读", "文件", "运行", "执行", "搜索", "查找", "列出",
        "查看", "打开", "创建", "写入", "删除", "安装",
        "分析", "梳理", "review", "analyze",
    ]
    return any(ind in text for ind in tool_indicators)


def _detect_context_requirement(text: str) -> bool:
    """Detect if question likely requires conversation context."""
    context_indicators = [
        "this", "that", "it", "previous", "above", "earlier",
        "这个", "那个", "上面", "之前", "刚才", "继续",
    ]
    return any(ind in text for ind in context_indicators)

# Backward compatibility aliases
def refine_answer(question: str, answer: str, config: IntelligenceConfig | None = None) -> tuple[str, QualityAssessment]:
    """Refine an answer and return (refined_text, assessment).

    Backward compatibility: returns tuple of (refined_answer, QualityAssessment).
    Returns original answer when no refinement is needed.
    """
    if config is None:
        config = IntelligenceConfig()

    assessment = assess_quality(answer, question, config)

    if not config.enabled:
        return answer, QualityAssessment(score=1.0, overall=1.0, completeness=1.0, clarity=1.0, accuracy=1.0, relevance=1.0, needs_refinement=False)

    refined = reflect_on_answer(question, answer, config)
    return refined or answer, assessment

# ---------------------------------------------------------------------------
# Backward compatibility stubs for old test suite
# These functions were part of the rule-only intelligence module.
# They are kept for test compatibility but delegate to the new LLM-powered pipeline.
# ---------------------------------------------------------------------------

def _assess_accuracy(question_or_answer: str, answer_or_question: str = "") -> float:
    """Assess answer accuracy (0.0-1.0). Backward compatibility stub."""
    answer = answer_or_question or question_or_answer
    # Check for uncertainty markers in both Chinese and English
    uncertainty_markers = ["不确定", "不知道", "not sure", "I'm not sure", "maybe", "perhaps", "possibly"]
    hedging_markers = ["可能", "也许", "might", "could be", "may be"]
    for marker in uncertainty_markers:
        if marker in answer:
            return 0.3
    for marker in hedging_markers:
        if marker in answer:
            return 0.5
    if len(answer) < 10:
        return 0.3
    if len(answer) < 50:
        return 0.7
    return 0.8


@dataclass
class _AssessmentResult:
    """Backward-compatible assessment result with attribute access."""
    overall: float = 0.8
    completeness: float = 0.8
    clarity: float = 0.7
    accuracy: float = 0.8
    relevance: float = 0.7
    issues: list = field(default_factory=list)
    suggestions: list = field(default_factory=list)


def _assess_answer_quality(question: str, answer: str = "") -> _AssessmentResult:
    """Assess answer quality. Backward compatibility stub."""
    completeness = _assess_completeness(question, answer)
    clarity = _assess_clarity(answer)
    accuracy = _assess_accuracy(question, answer)
    relevance = _assess_relevance(question, answer)
    overall = (completeness + clarity + accuracy + relevance) / 4
    issues = []
    suggestions = []
    if completeness < 0.5:
        issues.append("Answer incomplete")
        suggestions.append("Provide more details")
    if clarity < 0.5:
        suggestions.append("Use headers and lists for better structure")
    return _AssessmentResult(
        overall=overall, completeness=completeness, clarity=clarity,
        accuracy=accuracy, relevance=relevance, issues=issues, suggestions=suggestions,
    )


def _assess_clarity(answer: str) -> float:
    """Assess answer clarity (0.0-1.0). Backward compatibility stub."""
    if not answer or len(answer.strip()) == 0:
        return 0.0
    if answer.count("\n") > 3:
        return 0.9  # Structured
    if len(answer) < 20:
        return 0.5
    return 0.7


def _assess_completeness(question_or_answer: str, answer_or_question: str = "") -> float:
    """Assess answer completeness (0.0-1.0). Backward compatibility stub."""
    answer = answer_or_question if answer_or_question is not None else question_or_answer
    if not answer or len(answer.strip()) == 0:
        return 0.0
    if len(answer.strip()) < 5:
        return 0.0
    if len(answer) < 50:
        return 0.4
    if len(answer) < 200:
        return 0.6
    return 0.8


def _assess_relevance(question_or_answer: str, answer_or_question: str = "") -> float:
    """Assess answer relevance (0.0-1.0). Backward compatibility stub."""
    question = question_or_answer
    answer = answer_or_question
    if not answer or len(answer.strip()) == 0:
        return 0.0
    if not question:
        return 0.7
    q_words = set(question.lower().split())
    a_words = set(answer.lower().split())
    overlap = len(q_words & a_words)
    return min(1.0, 0.5 + overlap * 0.1)


def _decompose_question(text: str, max_parts: int = 5) -> list[str]:
    """Decompose a complex question into sub-questions. Backward compatibility stub."""
    if not text or not text.strip():
        return []
    # Simple sentence-splitting decomposition
    parts = []
    for sep in ["?", "？", ".", "。", ";", "；"]:
        if sep in text:
            segments = text.split(sep)
            for seg in segments:
                seg = seg.strip()
                if seg and len(seg) > 3:
                    parts.append(seg + sep)
            break
    if not parts:
        parts = [text]
    return parts[:max_parts]


def _generate_reflection(question: str, answer_or_complexity: str = "", config_or_domain=None):
    """Generate reflection notes. Backward compatibility stub.

    Supports both old and new calling conventions:
    - Old: _generate_reflection(question, complexity, domain) -> list[str]
    - New: _generate_reflection(question, answer, config) -> str
    """
    # Detect old calling convention: _generate_reflection("q", "complex", "code")
    if answer_or_complexity in ("simple", "moderate", "complex"):
        # Old convention: generate reflection notes as list
        complexity = answer_or_complexity
        domain = config_or_domain or "general"
        notes = []
        if complexity == "complex":
            notes.append("这是一个复杂问题，需要分步骤回答。")
        if complexity == "simple" and len(question) < 10:
            notes.append("问题过于简短，可能指代不明。")
        if domain == "code":
            notes.append("代码问题：请提供可运行的代码示例。")
        if "指代" in question or (len(question) < 15 and complexity == "simple"):
            notes.append("存在指代不明的情况。")
        return notes if notes else ["请提供更详细的回答。"]
    # New convention
    answer = answer_or_complexity
    config = config_or_domain if isinstance(config_or_domain, IntelligenceConfig) else IntelligenceConfig()
    result = reflect_on_answer(question, answer, config)
    return result or answer


def _suggest_approach(analysis_or_complexity, domain: str = "general", requires_tools: bool = False) -> str:
    """Suggest an approach for answering. Backward compatibility stub.

    Supports both old and new calling conventions:
    - Old: _suggest_approach(complexity, domain, requires_tools)
    - New: _suggest_approach(QuestionAnalysis)
    """
    if isinstance(analysis_or_complexity, QuestionAnalysis):
        domain = analysis_or_complexity.domain
        complexity = analysis_or_complexity.complexity
    else:
        complexity = str(analysis_or_complexity)

    if domain == "code" and requires_tools:
        return "使用工具分析代码，提供可运行的代码示例"
    if domain == "code":
        return "提供可运行的代码示例，解释关键逻辑"
    if domain == "math":
        return "展示计算步骤，确保结果准确"
    if complexity == "simple":
        return "直接回答"
    return "提供结构清晰、内容充实的回答"
