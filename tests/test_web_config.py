"""Tests for web configuration UI module."""
from __future__ import annotations

import pytest

from src.gateway_web_config import (
    ConfigField,
    ConfigTab,
    _deep_merge,
    _get_config_tabs,
    _get_nested_value,
    _render_field,
    _render_tab,
    _set_nested_value,
    get_config_schema,
    handle_config_get,
    handle_config_post,
    render_web_config_ui,
)


class TestConfigField:
    def test_field_creation(self):
        field = ConfigField(
            name="test.field",
            label="Test Field",
            field_type="text",
            description="A test field",
            default="default",
        )
        assert field.name == "test.field"
        assert field.label == "Test Field"
        assert field.field_type == "text"
        assert field.default == "default"

    def test_field_defaults(self):
        field = ConfigField(name="test", label="Test", field_type="text")
        assert field.description == ""
        assert field.required is False
        assert field.options == []


class TestConfigTab:
    def test_tab_creation(self):
        tab = ConfigTab(
            id="test",
            label="Test Tab",
            icon="🔧",
            description="A test tab",
        )
        assert tab.id == "test"
        assert tab.label == "Test Tab"
        assert tab.icon == "🔧"

    def test_tab_with_fields(self):
        field = ConfigField(name="test", label="Test", field_type="text")
        tab = ConfigTab(
            id="test",
            label="Test",
            icon="🔧",
            description="Test",
            fields=[field],
        )
        assert len(tab.fields) == 1


class TestGetConfigTabs:
    def test_returns_tabs(self):
        tabs = _get_config_tabs()
        assert len(tabs) > 0
        assert all(isinstance(t, ConfigTab) for t in tabs)

    def test_tabs_have_ids(self):
        tabs = _get_config_tabs()
        ids = [t.id for t in tabs]
        assert "upstream" in ids
        assert "context" in ids
        assert "intelligence" in ids
        assert "concurrency" in ids

    def test_tabs_have_fields(self):
        tabs = _get_config_tabs()
        for tab in tabs:
            assert len(tab.fields) > 0


class TestNestedValue:
    def test_get_simple_value(self):
        config = {"key": "value"}
        assert _get_nested_value(config, "key") == "value"

    def test_get_nested_value(self):
        config = {"a": {"b": {"c": "value"}}}
        assert _get_nested_value(config, "a.b.c") == "value"

    def test_get_missing_value(self):
        config = {"a": "value"}
        assert _get_nested_value(config, "b") is None

    def test_get_missing_nested(self):
        config = {"a": {"b": "value"}}
        assert _get_nested_value(config, "a.c") is None

    def test_set_simple_value(self):
        config = {}
        result = _set_nested_value(config, "key", "value")
        assert result["key"] == "value"

    def test_set_nested_value(self):
        config = {}
        result = _set_nested_value(config, "a.b.c", "value")
        assert result["a"]["b"]["c"] == "value"

    def test_set_overwrite(self):
        config = {"a": {"b": "old"}}
        result = _set_nested_value(config, "a.b", "new")
        assert result["a"]["b"] == "new"


class TestDeepMerge:
    def test_merge_simple(self):
        base = {"a": 1, "b": 2}
        update = {"b": 3, "c": 4}
        result = _deep_merge(base, update)
        assert result == {"a": 1, "b": 3, "c": 4}

    def test_merge_nested(self):
        base = {"a": {"x": 1, "y": 2}}
        update = {"a": {"y": 3, "z": 4}}
        result = _deep_merge(base, update)
        assert result == {"a": {"x": 1, "y": 3, "z": 4}}

    def test_merge_empty(self):
        base = {"a": 1}
        update = {}
        result = _deep_merge(base, update)
        assert result == {"a": 1}

    def test_merge_new_keys(self):
        base = {}
        update = {"a": {"b": "value"}}
        result = _deep_merge(base, update)
        assert result == {"a": {"b": "value"}}


class TestRenderField:
    def test_render_text_field(self):
        field = ConfigField(name="test", label="Test", field_type="text")
        html = _render_field(field)
        assert "input" in html
        assert "text" in html
        assert "Test" in html

    def test_render_number_field(self):
        field = ConfigField(
            name="num",
            label="Number",
            field_type="number",
            min_value=0,
            max_value=100,
        )
        html = _render_field(field)
        assert "number" in html
        assert 'min="0"' in html
        assert 'max="100"' in html

    def test_render_boolean_field(self):
        field = ConfigField(name="bool", label="Boolean", field_type="boolean")
        html = _render_field(field)
        assert "checkbox" in html
        assert "toggle" in html.lower()

    def test_render_select_field(self):
        field = ConfigField(
            name="select",
            label="Select",
            field_type="select",
            options=[{"value": "a", "label": "A"}, {"value": "b", "label": "B"}],
        )
        html = _render_field(field)
        assert "select" in html
        assert "A" in html
        assert "B" in html

    def test_render_password_field(self):
        field = ConfigField(name="pass", label="Password", field_type="password")
        html = _render_field(field)
        assert "password" in html

    def test_render_textarea_field(self):
        field = ConfigField(name="ta", label="Textarea", field_type="textarea")
        html = _render_field(field)
        assert "textarea" in html

    def test_render_with_value(self):
        field = ConfigField(name="test", label="Test", field_type="text")
        html = _render_field(field, "myvalue")
        assert "myvalue" in html

    def test_render_required(self):
        field = ConfigField(name="test", label="Test", field_type="text", required=True)
        html = _render_field(field)
        assert "required" in html
        assert "*" in html


class TestRenderTab:
    def test_render_tab(self):
        tab = ConfigTab(
            id="test",
            label="Test",
            icon="🔧",
            description="Test tab",
            fields=[ConfigField(name="test", label="Test", field_type="text")],
        )
        html = _render_tab(tab, {})
        assert "tab-test" in html
        assert "Test" in html
        assert "🔧" in html

    def test_render_tab_with_config(self):
        tab = ConfigTab(
            id="test",
            label="Test",
            icon="🔧",
            description="Test tab",
            fields=[ConfigField(name="test.field", label="Field", field_type="text")],
        )
        html = _render_tab(tab, {"test": {"field": "value"}})
        assert "value" in html


class TestRenderWebConfigUI:
    def test_renders_html(self):
        html = render_web_config_ui()
        assert "<!DOCTYPE html>" in html
        assert "Gateway" in html

    def test_renders_tabs(self):
        html = render_web_config_ui()
        assert "upstream" in html
        assert "context" in html

    def test_renders_with_config(self):
        config = {
            "upstream": {"url": "http://test.com"},
        }
        html = render_web_config_ui(config)
        assert "http://test.com" in html

    def test_contains_javascript(self):
        html = render_web_config_ui()
        assert "<script>" in html
        assert "tab-btn" in html


class TestHandleConfigGet:
    def test_returns_html(self):
        html = handle_config_get({})
        assert "<!DOCTYPE html>" in html


class TestHandleConfigPost:
    def test_updates_config(self):
        current = {"a": 1, "b": 2}
        update = {"b": 3, "c": 4}
        result = handle_config_post(update, current)
        assert result["a"] == 1
        assert result["b"] == 3
        assert result["c"] == 4

    def test_nested_update(self):
        current = {"upstream": {"url": "old", "key": "keep"}}
        update = {"upstream": {"url": "new"}}
        result = handle_config_post(update, current)
        assert result["upstream"]["url"] == "new"
        assert result["upstream"]["key"] == "keep"


class TestGetConfigSchema:
    def test_returns_list(self):
        schema = get_config_schema()
        assert isinstance(schema, list)

    def test_schema_has_tabs(self):
        schema = get_config_schema()
        assert len(schema) > 0

    def test_schema_tabs_have_fields(self):
        schema = get_config_schema()
        for tab in schema:
            assert "id" in tab
            assert "label" in tab
            assert "fields" in tab
            assert len(tab["fields"]) > 0

    def test_schema_fields_have_type(self):
        schema = get_config_schema()
        for tab in schema:
            for field in tab["fields"]:
                assert "name" in field
                assert "type" in field


@pytest.mark.integration
class TestWebConfigIntegration:
    def test_full_render_cycle(self):
        """Test complete rendering with realistic config."""
        config = {
            "upstream": {
                "url": "https://api.example.com",
                "api_key": "sk-test-123",
                "model": "gpt-4",
                "timeout": 60,
            },
            "context": {
                "enabled": True,
                "max_input_tokens": 1048576,
            },
            "intelligence": {
                "enabled": True,
            },
        }

        html = render_web_config_ui(config)
        assert "https://api.example.com" in html
        assert "1048576" in html

    def test_config_update_preserves_fields(self):
        """Test that config updates preserve existing fields."""
        current = {
            "upstream": {"url": "http://old.com", "model": "gpt-4"},
            "context": {"enabled": True},
        }
        update = {
            "upstream": {"url": "http://new.com"},
        }

        result = handle_config_post(update, current)
        assert result["upstream"]["url"] == "http://new.com"
        assert result["upstream"]["model"] == "gpt-4"
        assert result["context"]["enabled"] is True
