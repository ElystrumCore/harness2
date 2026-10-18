"""Trusted read/write pilot entry; runs only inside the isolated worker."""
import asyncio
import contextlib
import json
import sys
from pathlib import Path


def main():
    chuck, hub = sys.argv[1:3]
    sys.path[:0] = [chuck, hub]
    with contextlib.redirect_stdout(sys.stderr):
        from cn_cli.runner import ApprovalPolicy, ChuckRunner
        from cn_cli.policies import ChuckPolicy
        from cn_cli.tool_schemas import TOOLS_SCHEMA
        from framework.adapters.tool_policy import PolicyContext, install_policy_hook, BUDGET_SOURCE_LLM_CALL
        policy = ChuckPolicy()
        policy.max_tokens = 2048
        policy.tool_schema = [t for t in TOOLS_SCHEMA if t['function']['name'] in {'read_file', 'write_file', 'list_files'}]
        candidate = Path('/work/AGENTS.md').read_text()
        policy.system_prompt = policy.system_prompt + '\n\nTask-specific operating guidance:\n' + candidate
        context = PolicyContext.from_documents(
            {'metadata': {'id': 'hybrid-pilot'}, 'spec': {
                'tools': [{'id': 'git.read', 'mode': 'allow'}, {'id': 'git.write-worktree', 'mode': 'allow'}],
                'verification': {'protected_paths': ['ledger.csv', 'AGENTS.md', 'task.txt', 'events.jsonl']},
            }},
            {'spec': {'repositories': [{'allowed_paths': ['**'], 'denied_paths': ['.env', '*.sh']}],
                      'network_allowlist': [], 'budgets': {'model_calls': 8, 'tool_calls': 24,
                                                        'cost_usd': .25, 'output_tokens': 12000}}},
            runtime='chuck', scope_root=Path('/work'), approval_policy='approve_all',
            requested_model='claude-haiku-5-5', requested_provider='anthropic',
        )
        context.budget_source = BUDGET_SOURCE_LLM_CALL
        def customizer(agent):
            install_policy_hook(agent, context)
            return agent
        runner = ChuckRunner(provider='anthropic', model='claude-haiku-5-5',
                             approval_policy=ApprovalPolicy.APPROVE_ALL, max_turns=8,
                             timeout_seconds=240, policy=policy, agent_customizer=customizer,
                             event_sink=context.consume_llm_call, log_file='/work/events.jsonl')
        result = asyncio.run(runner.execute(Path('/work/task.txt').read_text()))
        payload = result.to_dict()
        payload['governance'] = context.summary()
    print(json.dumps(payload))


if __name__ == '__main__':
    main()
