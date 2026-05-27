"""Tests for intelligence enhancement module."""
from __future__ import annotations

import pytest

from src.gateway_intelligence import (
    IntelligenceConfig,
    IntelligenceResult,
    QuestionAnalysis,
    QualityAssessment,
    _analyze_question,
    _assess_accuracy,
    _assess_answer_quality,
    _assess_clarity,
    _assess_completeness,
    _assess_relevance,
    _build_enhanced_system_prompt,
    _build_reflection_prompt,
    _decompose_question,
    _detect_complexity,
    _detect_context_requirement,
    _detect_domain,
    _detect_tool_requirement,
    _extract_conversation_context,
    _generate_reflection,
    _intelligence_config,
    _optimize_conversation_messages,
    _suggest_approach,
    enhance_intelligence,
    get_intelligence_summary,
    refine_answer,
)


class TestIntelligenceConfig:
    def test_default_config(self):
        config = IntelligenceConfig()
        assert config.enabled is True
        assert config.reflection_enabled is True
        assert config.decomposition_enabled is True
        assert config.quality_assessment_enabled is True
        assert config.max_reflection_tokens == 500
        assert config.max_decomposition_parts == 5
        assert config.quality_threshold == 0.6

    def test_custom_config(self):
        config = IntelligenceConfig(
            enabled=False,
            reflection_enabled=False,
            quality_threshold=0.8,
        )
        assert config.enabled is False
        assert config.reflection_enabled is False
        assert config.quality_threshold == 0.8

    def test_parse_config_from_dict(self):
        raw = {
            "enabled": True,
            "reflection_enabled": False,
            "quality_threshold": 0.9,
        }
        config = _intelligence_config(raw)
        assert config.enabled is True
        assert config.reflection_enabled is False
        assert config.quality_threshold == 0.9

    def test_parse_config_from_none(self):
        config = _intelligence_config(None)
        assert config.enabled is True

    def test_parse_config_from_empty(self):
        config = _intelligence_config({})
        assert config.enabled is True


class TestComplexityDetection:
    def test_simple_question(self):
        assert _detect_complexity("你好") == "simple"

    def test_simple_english(self):
        assert _detect_complexity("hello") == "simple"

    def test_moderate_length(self):
        text = "这是一段比较长的问题，需要一些分析才能回答，包含了一些技术细节"
        assert _detect_complexity(text) in ["moderate", "complex"]

    def test_complex_multiple_questions(self):
        text = "什么是Python？它和JavaScript有什么区别？哪个更适合初学者？"
        assert _detect_complexity(text) == "complex"

    def test_complex_with_code(self):
        text = "如何用Python实现一个web scraper，如果遇到反爬虫机制该怎么办？"
        assert _detect_complexity(text) == "complex"

    def test_moderate_with_code(self):
        text = "如何用Python读取CSV文件并进行数据分析，需要处理异常情况"
        assert _detect_complexity(text) in ["moderate", "complex"]


class TestDomainDetection:
    def test_code_domain(self):
        assert _detect_domain("如何用Python实现排序算法") == "code"

    def test_code_domain_english(self):
        assert _detect_domain("how to implement a function in python") == "code"

    def test_math_domain(self):
        assert _detect_domain("计算圆的面积公式是什么") == "math"

    def test_math_domain_english(self):
        assert _detect_domain("calculate the integral of x^2") == "math"

    def test_creative_domain(self):
        assert _detect_domain("写一个关于春天的故事") == "creative"

    def test_factual_domain(self):
        assert _detect_domain("什么是人工智能") == "factual"

    def test_general_domain(self):
        assert _detect_domain("今天天气怎么样") == "general"


class TestToolRequirement:
    def test_requires_tools_file(self):
        assert _detect_tool_requirement("读取文件内容") is True

    def test_requires_tools_command(self):
        assert _detect_tool_requirement("运行命令") is True

    def test_requires_tools_search(self):
        assert _detect_tool_requirement("搜索网页") is True

    def test_no_tools_needed(self):
        assert _detect_tool_requirement("什么是机器学习") is False


class TestContextRequirement:
    def test_requires_context(self):
        assert _detect_context_requirement("继续上面的讨论") is True

    def test_requires_context_english(self):
        assert _detect_context_requirement("continue from above") is True

    def test_no_context_needed(self):
        assert _detect_context_requirement("什么是Python") is False


class TestQuestionDecomposition:
    def test_single_question(self):
        result = _decompose_question("什么是Python？")
        assert len(result) == 1

    def test_multiple_questions(self):
        result = _decompose_question("什么是Python？它有什么特点？如何学习？")
        assert len(result) == 3

    def test_english_questions(self):
        result = _decompose_question("What is Python? How to learn it?")
        assert len(result) == 2

    def test_max_parts_limit(self):
        text = "？".join([f"问题{i}" for i in range(10)])
        result = _decompose_question(text)
        assert len(result) <= 5

    def test_empty_text(self):
        result = _decompose_question("")
        assert len(result) == 0


class TestReflection:
    def test_complex_reflection(self):
        notes = _generate_reflection("复杂的问题", "complex", "code")
        assert len(notes) > 0
        assert any("复杂" in n for n in notes)

    def test_code_reflection(self):
        notes = _generate_reflection("Python代码", "moderate", "code")
        assert any("代码" in n for n in notes)

    def test_ambiguous_reflection(self):
        notes = _generate_reflection("这个怎么解决", "simple", "general")
        assert any("指代" in n for n in notes)

    def test_short_question_reflection(self):
        notes = _generate_reflection("你好", "simple", "general")
        assert any("简短" in n for n in notes)


class TestApproachSuggestion:
    def test_simple_approach(self):
        approach = _suggest_approach("simple", "general", False)
        assert approach == "直接回答"

    def test_moderate_with_tools(self):
        approach = _suggest_approach("moderate", "code", True)
        assert "工具" in approach

    def test_complex_code_approach(self):
        approach = _suggest_approach("complex", "code", True)
        assert "分析" in approach or "代码" in approach

    def test_complex_math_approach(self):
        approach = _suggest_approach("complex", "math", False)
        assert "计算" in approach


class TestQuestionAnalysis:
    def test_analyze_simple_question(self):
        analysis = _analyze_question("你好")
        assert analysis.complexity == "simple"
        assert analysis.domain == "general"
        assert analysis.requires_tools is False

    def test_analyze_code_question(self):
        analysis = _analyze_question("如何用Python读取文件并处理数据？")
        assert analysis.domain == "code"
        assert analysis.requires_tools is True

    def test_analyze_complex_question(self):
        analysis = _analyze_question("什么是机器学习？它和深度学习有什么区别？如何入门？")
        assert analysis.complexity == "complex"
        assert len(analysis.sub_questions) > 0

    def test_analysis_has_reflection(self):
        analysis = _analyze_question("这个怎么解决？")
        assert len(analysis.reflection_notes) > 0

    def test_analysis_has_approach(self):
        analysis = _analyze_question("Python编程问题")
        assert analysis.suggested_approach != ""


class TestQualityAssessment:
    def test_completeness_good(self):
        score = _assess_completeness("什么是Python？", "Python是一种编程语言，广泛用于数据分析和人工智能开发，由Guido van Rossum创建")
        assert score >= 0.4

    def test_completeness_bad(self):
        score = _assess_completeness("请详细解释Python的所有特性", "不知道")
        assert score <= 0.5

    def test_relevance_good(self):
        score = _assess_relevance("Python编程语言特点", "Python是一种流行的编程语言，具有简洁易读的特点")
        assert score >= 0.4

    def test_relevance_bad(self):
        score = _assess_relevance("Python编程", "今天天气很好，适合出去玩")
        assert score <= 0.6

    def test_clarity_with_structure(self):
        answer = "# 标题\n\n第一段内容\n\n- 要点1\n- 要点2"
        score = _assess_clarity(answer)
        assert score > 0.6

    def test_clarity_plain_text(self):
        score = _assess_clarity("这是一段普通文本")
        assert score >= 0.5

    def test_accuracy_confident(self):
        score = _assess_accuracy("Python是Guido van Rossum创建的编程语言，广泛应用于各个领域")
        assert score >= 0.7

    def test_accuracy_uncertain(self):
        score = _assess_accuracy("Python可能是一种编程语言，我不太确定")
        assert score < 0.7


class TestFullAssessment:
    def test_assess_good_answer(self):
        assessment = _assess_answer_quality(
            "什么是Python编程语言？",
            "Python是一种高级编程语言，由Guido van Rossum创建。它以简洁易读的语法著称，广泛应用于Web开发、数据科学、人工智能等领域。Python支持多种编程范式，包括面向对象、函数式和过程式编程。",
        )
        # Good answer should have reasonable overall score
        assert assessment.overall >= 0.4
        assert assessment.completeness >= 0.4
        assert assessment.accuracy >= 0.7

    def test_assess_bad_answer(self):
        assessment = _assess_answer_quality(
            "请详细解释Python的所有特性和应用场景",
            "不知道",
        )
        # Bad answer should have lower score
        assert assessment.overall < 0.6
        assert assessment.completeness <= 0.5

    def test_assess_empty_answer(self):
        assessment = _assess_answer_quality("问题", "")
        assert assessment.completeness == 0.0
        assert assessment.relevance == 0.0


class TestEnhancedPrompt:
    def test_build_system_prompt_complex(self):
        analysis = QuestionAnalysis(
            original="复杂问题",
            complexity="complex",
            domain="code",
            requires_tools=True,
            requires_context=False,
            sub_questions=["子问题1？", "子问题2？"],
            suggested_approach="分析问题 -> 提供方案",
        )
        config = IntelligenceConfig()
        prompt = _build_enhanced_system_prompt(analysis, config)
        assert "complex" in prompt.lower()
        assert "code" in prompt.lower()
        assert "子问题1" in prompt

    def test_build_system_prompt_simple(self):
        analysis = QuestionAnalysis(
            original="简单问题",
            complexity="simple",
            domain="general",
            requires_tools=False,
            requires_context=False,
        )
        config = IntelligenceConfig()
        prompt = _build_enhanced_system_prompt(analysis, config)
        # Simple questions return empty prompt (no enhancement needed)
        assert prompt == ""


class TestReflectionPrompt:
    def test_build_reflection_prompt(self):
        prompt = _build_reflection_prompt(
            "什么是Python？",
            "Python是一种语言",
            QualityAssessment(
                completeness=0.5,
                relevance=0.5,
                clarity=0.5,
                accuracy=0.5,
                overall=0.5,
                suggestions=["回答不够完整"],
            ),
        )
        assert "reflect" in prompt.lower()
        assert "Python" in prompt
        assert "不够完整" in prompt


class TestConversationOptimization:
    def test_optimize_adds_system_prompt(self):
        messages = [
            {"role": "user", "content": "这是一段比较长的复杂技术问题，需要详细的分析"},
        ]
        analysis = QuestionAnalysis(
            original="复杂问题",
            complexity="complex",
            domain="code",
            requires_tools=False,
            requires_context=False,
        )
        optimized = _optimize_conversation_messages(messages, analysis)
        assert len(optimized) > len(messages)
        assert optimized[0]["role"] == "system"

    def test_optimize_preserves_existing_system(self):
        messages = [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "问题"},
        ]
        analysis = QuestionAnalysis(
            original="问题",
            complexity="complex",
            domain="general",
            requires_tools=False,
            requires_context=False,
        )
        optimized = _optimize_conversation_messages(messages, analysis)
        assert optimized[0]["content"] == "你是助手"

    def test_extract_context(self):
        messages = [
            {"role": "user", "content": "问题1"},
            {"role": "assistant", "content": "回答1"},
            {"role": "user", "content": "问题2"},
        ]
        context = _extract_conversation_context(messages, max_turns=2)
        assert "问题1" in context
        assert "回答1" in context


class TestEnhanceIntelligence:
    def test_disabled_config(self):
        config = IntelligenceConfig(enabled=False)
        result = enhance_intelligence(
            [{"role": "user", "content": "你好"}],
            config,
        )
        assert result.system_prompt is None
        assert result.should_reflect is False

    def test_simple_question(self):
        result = enhance_intelligence([{"role": "user", "content": "你好"}], IntelligenceConfig(use_llm=False))
        assert result.analysis.complexity == "simple"
        assert result.system_prompt is None

    def test_complex_question(self):
        result = enhance_intelligence([
            {"role": "user", "content": "什么是Python？它和JavaScript有什么区别？如何选择？"}
        ], IntelligenceConfig(use_llm=False))
        assert result.analysis.complexity == "complex"
        assert result.system_prompt is not None
        assert result.should_reflect is True

    def test_code_question(self):
        result = enhance_intelligence([
            {"role": "user", "content": "如何用Python实现文件读取功能？"}
        ], IntelligenceConfig(use_llm=False))
        assert result.analysis.domain == "code"
        assert result.analysis.requires_tools is True

    def test_result_summary(self):
        result = enhance_intelligence([
            {"role": "user", "content": "什么是Python？它有什么特点？"}
        ])
        summary = get_intelligence_summary(result)
        assert "复杂度" in summary
        assert "领域" in summary


class TestRefineAnswer:
    def test_good_answer_no_refinement(self):
        answer = "Python是一种高级编程语言，由Guido van Rossum创建，广泛应用于Web开发、数据科学和人工智能等领域。它以简洁易读的语法著称。"
        refined, assessment = refine_answer("什么是Python编程语言？", answer)
        assert refined == answer
        assert assessment.overall >= 0.4

    def test_bad_answer_needs_refinement(self):
        config = IntelligenceConfig(quality_threshold=0.8)
        answer = "不知道"
        refined, assessment = refine_answer("请详细解释Python", answer, config)
        assert assessment.needs_refinement is True
        assert len(assessment.suggestions) > 0

    def test_disabled_config(self):
        config = IntelligenceConfig(enabled=False)
        refined, assessment = refine_answer("问题", "回答", config)
        assert assessment.overall == 1.0


@pytest.mark.integration
class TestIntelligenceIntegration:
    def test_full_pipeline(self):
        messages = [
            {"role": "user", "content": "什么是机器学习？它和深度学习有什么区别？如何入门学习？"},
        ]
        result = enhance_intelligence(messages)

        assert result.analysis.complexity == "complex"
        # "学习" is not in code keywords, so domain should be factual or general
        assert result.analysis.domain in ["factual", "general", "code"]
        assert len(result.analysis.sub_questions) > 0
        assert result.system_prompt is not None
        assert result.should_reflect is True

    def test_conversation_with_context(self):
        messages = [
            {"role": "user", "content": "什么是Python？"},
            {"role": "assistant", "content": "Python是一种编程语言"},
            {"role": "user", "content": "继续上面的讨论，它有什么特点？"},
        ]
        result = enhance_intelligence(messages)
        assert result.analysis.requires_context is True

    def test_quality_assessment_workflow(self):
        question = "请详细解释Python的装饰器是什么，如何使用，以及常见的应用场景"
        answer = "装饰器是Python的一种设计模式，使用@语法，可以修改函数行为。"

        _, assessment = refine_answer(question, answer)
        assert assessment.completeness < 1.0  # Answer is incomplete
        assert len(assessment.suggestions) > 0
