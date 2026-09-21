"""Update the maintained fork in isolation; publish only after regression checks."""

import argparse
import json
import os
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

TESTS = [
	'tests/ci/browser/test_session_manager.py',
	'tests/ci/browser/test_frame_collection.py',
	'tests/ci/test_downloads_streaming.py',
	'tests/ci/test_downloads_streaming_browser.py',
	'tests/ci/test_remote_download_complete_callback.py',
	'tests/ci/test_agent_download_paths.py',
	'tests/ci/test_downloads_watchdog.py',
	'tests/ci/security/test_download_filename_sanitization.py',
]


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument('--publish', action='store_true', help='Update remote main after checks pass')
	parser.add_argument('--strategy', choices=['merge', 'rebase'], default='merge')
	parser.add_argument('--output', type=Path, required=True, help='New directory for checkout and evidence')
	args = parser.parse_args()
	output = args.output.resolve()
	output.mkdir(parents=True, exist_ok=False)
	checkout = output / 'checkout'
	summary = {'strategy': args.strategy, 'published': False, 'passed': False}
	env = os.environ.copy()
	env.pop('VIRTUAL_ENV', None)
	env['GIT_TERMINAL_PROMPT'] = '0'
	env['GIT_MERGE_AUTOEDIT'] = 'no'
	env['ANONYMIZED_TELEMETRY'] = 'false'

	def run(*command, cwd=checkout, timeout=1800):
		with (output / 'run.log').open('a') as log:
			log.write('\n$ ' + ' '.join(map(str, command)) + '\n')
			log.flush()
			result = subprocess.run(command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
		result.check_returncode()

	def git_value(*arguments):
		return subprocess.check_output(['git', *arguments], cwd=checkout, env=env, text=True, timeout=60).strip()

	try:
		run(
			'git',
			'clone',
			'--no-checkout',
			'--single-branch',
			'--branch',
			'main',
			'https://github.com/254808127/browser-use.git',
			str(checkout),
			cwd=output,
		)
		run('git', 'checkout', '--detach', 'origin/main')
		old = git_value('rev-parse', 'HEAD')
		summary['previous_head'] = old
		run('git', 'remote', 'add', 'upstream', 'https://github.com/browser-use/browser-use.git')
		run('git', 'fetch', '--no-tags', 'upstream', 'main')
		upstream = git_value('rev-parse', 'upstream/main')
		summary['upstream_head'] = upstream
		if subprocess.run(['git', 'merge-base', '--is-ancestor', upstream, old], cwd=checkout).returncode == 0:
			summary.update(passed=True, unchanged=True, candidate_head=old)
			return
		run('git', 'config', 'user.name', 'browser-use fork maintenance')
		run('git', 'config', 'user.email', '4832404+254808127@users.noreply.github.com')
		if args.strategy == 'rebase':
			run('git', 'rebase', '--rebase-merges', 'upstream/main')
		else:
			run('git', 'merge', '--no-edit', 'upstream/main')
		candidate = git_value('rev-parse', 'HEAD')
		summary['candidate_head'] = candidate
		run('git', 'diff', '--check', 'upstream/main', 'HEAD')
		run('uv', 'sync', '--dev')
		run('uv', 'run', '--no-sync', 'pre-commit', 'run', '--from-ref', 'upstream/main', '--to-ref', 'HEAD')
		run('uv', 'run', '--no-sync', 'pytest', '-o', 'addopts=', '-q', '--junitxml=' + str(output / 'regressions.xml'), *TESTS)
		cases = ET.parse(output / 'regressions.xml').findall('.//testcase')
		if len(cases) < 51 or any(case.find(tag) is not None for case in cases for tag in ('failure', 'error', 'skipped')):
			raise RuntimeError('Regression gate requires at least 51 passing tests and no skips')
		if git_value('status', '--porcelain', '--untracked-files=no'):
			raise RuntimeError('Checks changed tracked files; review candidate manually')
		summary.update(passed=True, tests_passed=len(cases))
		if args.publish:
			stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
			backup = f'refs/heads/backup/main-{stamp}-{old[:12]}'
			# Backup and update succeed together; lease rejects concurrent main updates.
			run(
				'git',
				'push',
				'--atomic',
				f'--force-with-lease=refs/heads/main:{old}',
				'origin',
				f'{old}:{backup}',
				f'{candidate}:refs/heads/main',
			)
			summary.update(published=True, backup_ref=backup)
	finally:
		(output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
		print(json.dumps(summary, indent=2))


if __name__ == '__main__':
	main()
