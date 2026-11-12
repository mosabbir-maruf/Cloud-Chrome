"""Mocked tests for the GHCR package retention script."""

import json
import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ghcr-retain-current.sh"
DIGEST = "sha256:" + "a" * 64
SHA_TAG = "sha-" + "b" * 40


def version(version_id, digest, tags):
    return {"id": version_id, "name": digest, "metadata": {"container": {"tags": tags}}}


def run_script(pages, delete_status=None, final_pages=None, inventory_status=None):
    with tempfile.TemporaryDirectory() as temporary:
        temp_path = pathlib.Path(temporary)
        fakebin = temp_path / "bin"
        fakebin.mkdir()
        curl = fakebin / "curl"
        curl.write_text(
            """#!/usr/bin/env python3
import json, os, pathlib, sys, urllib.parse
args = sys.argv[1:]
def arg(name): return args[args.index(name) + 1]
method = arg('--request')
url = args[-1]
parsed = urllib.parse.urlparse(url)
query = urllib.parse.parse_qs(parsed.query)
page = int(query.get('page', ['1'])[0])
state_path = pathlib.Path(os.environ['MOCK_STATE'])
state = json.loads(state_path.read_text())
headers = pathlib.Path(arg('--dump-header'))
out = pathlib.Path(arg('--output'))
if method == 'DELETE':
    version_id = int(parsed.path.rsplit('/', 1)[1])
    if state['delete_status']:
        headers.write_text('HTTP/1.1 403 Forbidden\\r\\n')
        out.write_text(state['delete_status'])
        sys.stdout.write('403')
        sys.exit(0)
    state['deleted'].append(version_id)
    if state['final_pages'] is None:
        entries = [entry for group in state['pages'] for entry in group if entry['id'] != version_id]
        state['pages'] = [entries]
    else:
        entries = [entry for group in state['final_pages'] for entry in group if entry['id'] != version_id]
        state['final_pages'] = [entries]
    state_path.write_text(json.dumps(state))
    headers.write_text('HTTP/1.1 204 No Content\\r\\n')
    out.write_text('')
    sys.stdout.write('204')
    sys.exit(0)
if state['inventory_status'] and not state['deleted']:
    headers.write_text('HTTP/1.1 404 Not Found\\r\\n')
    out.write_text(state['inventory_status'])
    sys.stdout.write('404')
    sys.exit(0)
if state['final_pages'] is not None and state['deleted']:
    selected = state['final_pages']
elif state['deleted']:
    entries = [entry for group in state['pages'] for entry in group]
    selected = [entries]
else:
    selected = state['pages']
if state['deleted'] and state['final_pages'] is None:
    body = selected[0] if page == 1 else []
else:
    body = selected[page - 1] if page <= len(selected) else []
headers.write_text('HTTP/1.1 200 OK\\r\\n')
out.write_text(json.dumps(body))
sys.stdout.write('200')
""",
            encoding="utf-8",
        )
        curl.chmod(0o755)
        state = {
            "pages": pages,
            "final_pages": final_pages,
            "delete_status": delete_status,
            "inventory_status": inventory_status,
            "deleted": [],
        }
        state_path = temp_path / "state.json"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{fakebin}:{env['PATH']}",
                "MOCK_STATE": str(state_path),
                "GH_TOKEN": "mock-token",
                "API_URL": "https://api.github.test",
                "GHCR_DIGEST": DIGEST,
                "EXPECTED_SHA_TAG": SHA_TAG,
                "PACKAGE_OWNER": "mosabbir-maruf",
                "PACKAGE_NAME": "cloud-chrome",
                "RUNNER_TEMP": temporary,
            }
        )
        result = subprocess.run(["bash", str(SCRIPT)], env=env, text=True, capture_output=True)
        saved_state = json.loads(state_path.read_text(encoding="utf-8"))
        return result, saved_state


class RetentionScriptTest(unittest.TestCase):
    def current(self, version_id=7):
        return version(version_id, DIGEST, ["latest", SHA_TAG])

    def test_paginates_all_versions_and_deletes_older_ids(self):
        pages = [
            [version(i, "sha256:" + format(i, "064x"), [f"old-{i}"]) for i in range(100)],
            [self.current()],
        ]
        pages[0][7] = version(107, "sha256:" + format(107, "064x"), ["old-107"])
        result, state = run_script(pages)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(state["deleted"]), [i for i in range(100) if i != 7] + [107])
        self.assertIn("Verified exactly one package version remains", result.stdout)

    def test_missing_inventory_or_api_failure_never_deletes(self):
        result, state = run_script([], inventory_status="404 Not Found")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["deleted"], [])

    def test_missing_current_version_never_deletes(self):
        result, state = run_script([[version(1, "sha256:" + "c" * 64, ["latest"])]] )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["deleted"], [])

    def test_ambiguous_current_digest_never_deletes(self):
        pages = [[self.current(7), self.current(8)]]
        result, state = run_script(pages)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["deleted"], [])

    def test_delete_failure_stops_and_fails(self):
        pages = [[version(1, "sha256:" + "c" * 64, ["old"]), self.current()]]
        result, state = run_script(pages, delete_status="403 Forbidden")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["deleted"], [])
        self.assertIn("Could not delete old version ID", result.stderr)

    def test_post_deletion_inventory_must_confirm_exact_current_version(self):
        pages = [[version(1, "sha256:" + "c" * 64, ["old"]), self.current()]]
        result, state = run_script(
            pages,
            final_pages=[[version(7, DIGEST, ["latest"])]],
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["deleted"], [1])
        self.assertIn("final version digest or tags", result.stderr)


if __name__ == "__main__":
    unittest.main()
