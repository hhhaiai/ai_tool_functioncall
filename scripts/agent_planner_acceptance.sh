#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

run() {
  printf '\n==> %s\n' "$*"
  "$@"
}

printf 'Agent Planner acceptance gate\n'
printf 'repo: %s\n' "$ROOT"

FULL=0
CLI=0
for arg in "$@"; do
  case "$arg" in
    --full) FULL=1 ;;
    --cli) CLI=1 ;;
    --help|-h)
      printf 'usage: %s [--full] [--cli]\n' "$0"
      printf '  --full  run full pytest suite after smoke gate\n'
      printf '  --cli   require real Claude Code and Codex CLI smoke\n'
      exit 0
      ;;
    *)
      printf 'unknown argument: %s\n' "$arg" >&2
      exit 2
      ;;
  esac
done
printf 'mode: smoke%s%s\n' "$([[ "$FULL" == "1" ]] && printf '+full')" "$([[ "$CLI" == "1" ]] && printf '+cli')"

run python3 tests/integration/agent_planner_synthesis_guard_smoke.py
run python3 tests/integration/agent_planner_project_analysis_smoke.py
run python3 tests/integration/agent_planner_multiround_smoke.py
run python3 tests/integration/agent_planner_protocol_strict_smoke.py
run python3 tests/integration/agent_planner_public_surface_smoke.py
run python3 tests/integration/agent_planner_remote_pressure_smoke.py
run python3 tests/integration/agent_planner_long_context_pressure_smoke.py

run python3 -m pytest -q \
  tests/test_gateway_assistants.py \
  tests/test_gateway_proxy_errors.py \
  tests/test_agent_planner_client_context.py \
  tests/test_gateway.py::NativeGatewayTests::test_models_and_count_tokens_endpoints_for_claude_code_compatibility \
  tests/test_gateway.py::NativeGatewayTests::test_agent_runtime_audit_scope_contract_documents_non_conversation_exclusions \
  tests/test_gateway.py::NativeGatewayTests::test_gateway_internal_workspace_fields_are_not_forwarded_upstream \
  tests/test_gateway.py::NativeGatewayTests::test_streaming_passthrough_strips_internal_workspace_fields \
  tests/test_gateway.py::NativeGatewayTests::test_extracts_legacy_chat_function_call_and_appends_function_result \
  tests/test_gateway.py::NativeGatewayTests::test_legacy_chat_function_result_becomes_planner_evidence \
  tests/test_gateway.py::NativeGatewayTests::test_failed_chat_tool_result_marks_planner_evidence_error \
  tests/test_gateway.py::NativeGatewayTests::test_failed_responses_tool_output_marks_planner_evidence_error \
  tests/test_gateway.py::NativeGatewayTests::test_responses_function_call_output_becomes_planner_evidence_with_name_and_args \
  tests/test_gateway.py::NativeGatewayTests::test_responses_custom_tool_output_becomes_planner_evidence_with_string_input \
  tests/test_gateway.py::NativeGatewayTests::test_failed_chat_tool_result_marker_is_not_forwarded_to_final_synthesis \
  tests/test_gateway.py::NativeGatewayTests::test_failed_responses_tool_output_marker_is_not_forwarded_to_final_synthesis \
  tests/test_gateway.py::NativeGatewayTests::test_responses_tool_response_sets_chat_finish_reason_tool_calls \
  tests/test_gateway.py::NativeGatewayTests::test_responses_custom_tool_response_converts_to_chat_tool_call \
  tests/test_gateway.py::NativeGatewayTests::test_response_has_tool_calls_detects_responses_custom_tool_call \
  tests/test_gateway.py::NativeGatewayTests::test_responses_custom_tool_history_converts_to_chat_messages \
  tests/test_gateway.py::NativeGatewayTests::test_responses_codex_builtin_tool_history_converts_to_chat_messages \
  tests/test_gateway.py::NativeGatewayTests::test_responses_codex_builtin_tool_response_converts_to_chat_tool_call \
  tests/test_gateway.py::NativeGatewayTests::test_responses_codex_builtin_tool_output_becomes_planner_evidence \
  tests/test_gateway.py::ProtocolConversionTests::test_anthropic_tool_result_error_roundtrips_through_chat_marker \
  tests/test_gateway.py::ProtocolConversionTests::test_openai_chat_legacy_function_call_response_to_anthropic_tool_use \
  tests/test_gateway.py::ProtocolConversionTests::test_openai_chat_tool_call_non_object_arguments_wrap_for_anthropic \
  tests/test_gateway.py::ProtocolConversionTests::test_openai_chat_legacy_function_history_converts_to_responses \
  tests/test_gateway.py::StreamingToolEventTests::test_detect_openai_legacy_function_call_delta \
  tests/test_gateway.py::StreamingToolEventTests::test_detect_responses_custom_tool_call_item \
  tests/test_gateway.py::StreamingToolEventTests::test_detect_responses_codex_builtin_tool_call_item \
  tests/test_gateway.py::NativeGatewayTests::test_gateway_owned_builtin_calculator_preexecutes_without_request_tools \
  tests/test_gateway.py::NativeGatewayTests::test_gateway_owned_multiple_builtin_tools_preexecute_without_request_tools \
  tests/test_gateway.py::NativeGatewayTests::test_configured_mcp_tool_preexecutes_without_request_tools \
  tests/test_gateway.py::NativeGatewayTests::test_admin_skill_delete_rejects_path_traversal_and_cross_origin \
  tests/test_gateway.py::NativeGatewayTests::test_declared_gateway_builtin_name_is_downstream_owned \
  tests/test_gateway.py::NativeGatewayTests::test_direct_user_side_tool_call_requires_downstream_client_by_default \
  tests/test_gateway.py::NativeGatewayTests::test_direct_image_generation_is_gateway_owned_provider_tool \
  tests/test_gateway.py::NativeGatewayTests::test_declared_downstream_file_path_does_not_use_gateway_env_without_client_workspace \
  tests/test_gateway.py::NativeGatewayTests::test_declared_downstream_file_path_anchors_to_explicit_client_workspace \
  tests/test_gateway.py::NativeGatewayTests::test_relative_workspace_value_is_not_treated_as_gateway_service_path \
  tests/test_gateway.py::NativeGatewayTests::test_remote_identity_without_workspace_does_not_fall_back_to_gateway_env_root \
  tests/test_gateway.py::NativeGatewayTests::test_downstream_client_id_without_workspace_does_not_use_gateway_env_root \
  tests/test_gateway.py::NativeGatewayTests::test_downstream_client_id_overrides_body_client_id_for_runtime_scope \
  tests/test_gateway.py::NativeGatewayTests::test_streaming_downstream_client_id_without_workspace_does_not_use_gateway_env_root \
  tests/test_gateway.py::NativeGatewayTests::test_gateway_owned_public_helpers_scope_downstream_client_id_without_workspace \
  tests/test_gateway.py::NativeGatewayTests::test_gateway_owned_post_routes_pass_downstream_key_as_client_id \
  tests/test_gateway.py::NativeGatewayTests::test_tool_result_cache_keys_include_runtime_scope_not_only_workspace \
  tests/test_semantic_cache.py::TestSemanticCache::test_scope_key_isolates_exact_and_semantic_matches \
  tests/test_cache_persistence.py::TestCachePersistence::test_semantic_cache_scope_persists_and_isolates \
  tests/test_gateway.py::NativeGatewayTests::test_remote_identity_memory_without_scope_does_not_use_gateway_env_root \
  tests/test_gateway.py::NativeGatewayTests::test_memory_tool_scopes_by_authenticated_client_id_and_blocks_global_listing \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_text_tool_fallback_surfaces_declared_user_side_tool \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_uses_history_only_for_followup_not_plain_thanks \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_plain_thanks_after_project_history_does_not_dispatch_project_tool \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_uses_history_for_explicit_project_followup \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_history_validation_does_not_pollute_plain_followup \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_session_key_without_scope_does_not_use_gateway_env_root_for_remote_identity \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_code_search_without_scope_does_not_infer_gateway_service_project \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_injects_compact_evidence_before_final_upstream_synthesis \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_evidence_survives_context_compaction \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_chat_only_final_synthesis_ignores_upstream_json_tool_request \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_chat_only_declared_calculator_collision_surfaces_downstream_tool \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_chat_only_custom_function_call_infers_json_schema_arguments \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_does_not_leak_chat_only_refusal_after_evidence \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_does_not_leak_cross_session_path_drift_after_evidence \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_does_not_leak_final_synthesis_nonanswer_after_evidence

if [[ "$CLI" == "1" ]]; then
  run python3 tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
fi

if [[ "$FULL" == "1" ]]; then
  printf '
==> GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN=0 python3 -m pytest -ra tests
'
(
  export GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN=0
  python3 -m pytest -ra tests
)
fi

printf '\nAgent Planner acceptance gate: PASS\n'
