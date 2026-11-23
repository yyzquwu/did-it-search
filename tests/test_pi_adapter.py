import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


NODE = shutil.which("node")
ADAPTER = Path(__file__).resolve().parents[1] / "src" / "diditsearch" / "pi_adapter.mjs"


@unittest.skipUnless(NODE, "Node.js is required for Pi adapter tests")
class PiAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        version = subprocess.check_output([NODE, "--version"], text=True, timeout=15).strip()
        if tuple(map(int, version.removeprefix("v").split(".")[:2])) < (22, 19):
            raise unittest.SkipTest("Pi adapter tests require Node.js 22.19+")

    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="pi adapter # ")))
        self.fixture = self.root / "fixture"
        self.fixture.mkdir()
        self.module = self.root / "grep.mjs"
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("DIDITSEARCH_PI_")}
        self.env["DIDITSEARCH_PI_MODULE"] = str(self.module)

    def write_tool(self, body, export="createGrepTool", properties="pattern, literal, path, glob"):
        props = json.dumps(dict.fromkeys(properties.split(", "), {}))
        self.module.write_text(
            f"export function {export}(cwd) {{\n"
            f"  return {{ parameters: {{ properties: {props} }}, async execute(id, args) {{\n"
            f"    {body}\n"
            "  } };\n"
            "}\n",
            encoding="utf-8",
        )

    def run_adapter(self, request=None):
        if request is None:
            request = {"pattern": "café.*", "path": ".", "glob": None}
        return subprocess.run(
            [NODE, str(ADAPTER)], input=json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n",
            cwd=self.fixture, env=self.env, capture_output=True, timeout=15,
        )

    def response(self, request=None):
        run = self.run_adapter(request)
        self.assertEqual(run.returncode, 0, run.stderr.decode("utf-8"))
        return json.loads(run.stdout)

    def test_literal_arguments_use_fixture_cwd_and_keep_native_match(self):
        self.env["DIDITSEARCH_PI_ROOT"] = "unused-root-when-pinned"
        self.write_tool(
            "if (cwd !== process.cwd() || process.env.PI_OFFLINE !== '1' || id !== 'did-it-search')"
            " throw new Error('wrong execution context');\n"
            "if (JSON.stringify(args) !== JSON.stringify({ pattern: 'café.*', literal: true,"
            " path: 'src', glob: './lib/*.js' })) throw new Error('wrong arguments');\n"
            "return { content: [{ type: 'text', text: 'lib/app.js:12: café.*  \\n' }],"
            " details: { linesTruncated: true } };"
        )
        out = self.response({"pattern": "café.*", "path": "src", "glob": "./lib/*.js", "limit": 1})
        self.assertEqual(out, {
            "status": "matched", "detail": "lib/app.js:12: café.*  \n",
            "raw": {"content": [{"type": "text", "text": "lib/app.js:12: café.*  \n"}],
                    "details": {"linesTruncated": True}},
        })

    def test_empty_result_keeps_native_text_with_no_skip_inference(self):
        self.write_tool(
            "if ('glob' in args && args.glob === null) throw new Error('null glob was forwarded');"
            "return { content: [{ type: 'text', text: 'No matches found' }], details: undefined };",
            export="createGrepToolDefinition",
        )
        expected = {"status": "no_match", "detail": "No matches found",
                    "raw": {"content": [{"type": "text", "text": "No matches found"}]}}
        self.assertEqual(self.response(), expected)
        self.assertEqual(self.response({"pattern": "literal", "path": "absent", "glob": "*.not-used"}), expected)

    def test_missing_path_is_reported_only_from_native_error(self):
        self.write_tool("throw new Error('Path not found: /somewhere/source');")
        self.assertEqual(self.response(), {
            "status": "missing_path", "detail": "Path not found: /somewhere/source",
            "raw": {"name": "Error", "message": "Path not found: /somewhere/source"},
        })
        self.write_tool("return { isError: true, content: [{ type: 'text', text: 'Path not found: source' }] };")
        self.assertEqual(self.response()["status"], "missing_path")

    def test_operational_failures_and_unknown_text_are_adapter_errors(self):
        for body, message in [
            ("throw new Error('Failed to run ripgrep: access denied');", "access denied"),
            ("return {content: [{type: 'text', text: 'Search failed'}]};", "Unrecognized Pi grep output"),
            ("return {content: []};", "returned no text content"),
            ("return {isError: true, content: [{type: 'text', text: 'file:1: error'}]};", "file:1: error"),
        ]:
            with self.subTest(body=body):
                self.write_tool(body)
                run = self.run_adapter()
                self.assertEqual((run.returncode, run.stdout), (2, b""))
                self.assertIn(message, run.stderr.decode("utf-8"))

    def test_incompatible_tool_does_not_silently_drop_literal_or_glob(self):
        for properties, message in [("pattern, path", "literal search"), ("pattern, literal, path", "glob")]:
            with self.subTest(properties=properties):
                self.write_tool("throw new Error('must validate before calling Pi');", properties=properties)
                run = self.run_adapter({"pattern": "literal", "path": ".", "glob": "*.rs"})
                self.assertEqual((run.returncode, run.stdout), (2, b""))
                self.assertIn(f"Pi grep does not expose {message}", run.stderr.decode("utf-8"))

    def test_installed_package_import_is_anchored_at_the_caller(self):
        project = self.root / "invoking project"
        pkg = project / "node_modules" / "@earendil-works" / "pi-coding-agent"
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text('{"exports":{".":{"import":"./sdk.mjs"}}}', encoding="utf-8")
        self.write_tool("return { content: [{ type: 'text', text: 'file:1: from caller package' }] };")
        self.module.rename(pkg / "sdk.mjs")
        del self.env["DIDITSEARCH_PI_MODULE"]
        self.env["DIDITSEARCH_PI_ROOT"] = str(project)
        self.assertEqual(self.response()["detail"], "file:1: from caller package")

    def test_module_override_must_be_absolute(self):
        self.env["DIDITSEARCH_PI_MODULE"] = "grep.mjs"
        run = self.run_adapter()
        self.assertEqual((run.returncode, run.stdout), (2, b""))
        self.assertIn(b"DIDITSEARCH_PI_MODULE must be an absolute", run.stderr)

    def test_protocol_limit_prefers_native_text_over_duplicate_raw(self):
        self.write_tool("return { content: [{ type: 'text', text: 'file:1: ' + 'x'.repeat(40000) }] };")
        self.assertEqual(self.response(), {"status": "matched", "detail": "file:1: " + "x" * 40000})
