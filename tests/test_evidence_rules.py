import os
import subprocess
import unittest
from contextlib import chdir
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from diditsearch.evidence_rules import _git_path, git_rules


def rule(line, pattern, source=".gitignore"):
    return {"source": source, "line": line, "pattern": pattern}


@unittest.skipUnless(_git_path(), "Git is unavailable")
class EvidenceRuleTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.root = self.base / "repo"
        home = self.base / "home"
        home.mkdir()
        self.enterContext(patch.dict(os.environ, {
            **{key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")},
            "HOME": str(home), "USERPROFILE": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
        }, clear=True))
        self.init(self.root)

    def init(self, path):
        subprocess.run([_git_path(), "init", "-q", "--template=", path], check=True, timeout=15)

    def put(self, name, data=b""):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def rules(self, *paths):
        return git_rules(paths, self.root, lambda: 15)

    def test_reports_file_directory_and_nested_source(self):
        self.put(".gitignore", b"generated/\n*.cache\n")
        self.put("pkg/.gitignore", b"# comment\n*.js\n")
        generated = self.put("generated/client.ts")
        nested = self.put("pkg/odd [name] caf\u00e9.js")
        cache = self.put("-sample.cache")
        kept = self.put("source.ts")
        self.assertEqual(self.rules(generated, generated.parent, nested, cache, kept), {
            generated: rule(1, "generated/"), generated.parent: rule(1, "generated/"),
            nested: rule(2, "*.js", "pkg/.gitignore"), cache: rule(2, "*.cache"),
        })

    @unittest.skipIf(os.name == "nt", "POSIX filename characters")
    def test_nul_protocol_preserves_newlines_tabs_and_pathspec_prefixes(self):
        self.put(".gitignore", b"*.tmp\n")
        path = self.put(":(exclude)tab\tnewline\n.tmp")
        self.assertEqual(self.rules(path), {path: rule(1, "*.tmp")})

    def test_reports_escaped_literal_but_omits_hidden_and_negated_matches(self):
        self.put(".gitignore", b"*.tmp\n!keep.tmp\n\\!literal\nprivate/\n")
        literal = self.put("!literal")
        negated = self.put("keep.tmp")
        hidden = self.put(".hidden.tmp")
        hidden_parent = self.put(".dir/inside.tmp")
        self.assertEqual(self.rules(literal, negated, hidden, hidden_parent), {literal: rule(3, r"\!literal")})

    def test_omits_ambiguous_pattern_syntax(self):
        # ripgrep understands brace alternatives, while Git treats them literally.
        self.put(".gitignore", b"*.tmp\n{one,two}.tmp\n")
        self.assertEqual(self.rules(self.put("one.tmp")), {})

    @unittest.skipUnless(os.name == "nt", "Windows case-insensitive paths")
    def test_omits_gitignore_with_noncanonical_case(self):
        self.put(".gitignore", b"*.tmp\n")
        self.put("pkg/.GITIGNORE", b"one.tmp\n")
        if not (self.root / "pkg/.gitignore").exists():
            self.skipTest("The filesystem uses case-sensitive paths")
        self.assertEqual(self.rules(self.put("pkg/one.tmp")), {})

    def test_omits_custom_ignore_and_nested_repo_ambiguity(self):
        self.put(".gitignore", b"*.tmp\n")
        ordinary = self.put("ordinary.tmp")
        nested = self.put("nested/ignored.tmp")
        customized = self.put("custom/ignored.tmp")
        self.init(nested.parent)
        for name in (".ignore", ".rgignore"):
            with self.subTest(name=name):
                self.put(f"custom/{name}", b"ignored.tmp\n")
                self.assertEqual(self.rules(ordinary, nested, customized), {ordinary: rule(1, "*.tmp")})
                (self.base / name).write_bytes(b"repo/\n")
                self.assertEqual(self.rules(ordinary), {})
                (self.base / name).unlink()
                (customized.parent / name).unlink()
        self.assertEqual(git_rules([nested], nested.parent, lambda: 15), {})

    def test_omits_info_exclude_and_possible_global_rules(self):
        self.put(".gitignore", b"*.tmp\n")
        self.put(".git/info/exclude", b"info.log\npruned/\n")
        local = self.put("local.tmp")
        excluded = self.put("info.log")
        pruned = self.put("pruned/inside.tmp")
        self.assertEqual(self.rules(local, excluded, pruned), {local: rule(1, "*.tmp")})
        global_rule = Path(os.environ["XDG_CONFIG_HOME"]) / "git/ignore"
        global_rule.parent.mkdir(parents=True)
        global_rule.write_bytes(b"*.log\npruned/\n")
        self.assertEqual(self.rules(local, excluded, pruned), {})

    def test_drops_ambient_git_repository_and_configuration_overrides(self):
        self.put(".gitignore", b"*.tmp\n")
        path = self.put("ignored.tmp")
        foreign = self.base / "foreign"
        self.init(foreign)
        with patch.dict(os.environ, {"GIT_DIR": str(foreign / ".git"), "GIT_WORK_TREE": str(foreign),
                                     "GIT_LITERAL_PATHSPECS": "1", "GIT_CONFIG_COUNT": "3"}):
            self.assertEqual(self.rules(path), {path: rule(1, "*.tmp")})
        with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(foreign / ".git/config")}):
            self.assertEqual(self.rules(path), {})

    def test_ignores_symlink_paths_and_ignore_sources(self):
        self.put(".gitignore", b"*.tmp\n")
        target = self.put("ordinary.tmp")
        linked = self.root / "linked.tmp"
        folder = self.root / "folder"
        folder.mkdir()
        try:
            linked.symlink_to(target)
            (folder / ".gitignore").symlink_to(self.root / ".gitignore")
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"Cannot create symlinks: {exc}")
        self.assertEqual(self.rules(target, linked, self.put("folder/ambiguous.tmp")), {target: rule(1, "*.tmp")})

    def test_missing_git_and_propagated_deadline(self):
        self.put(".gitignore", b"ignored\n")
        path = self.put("ignored")
        exe = self.put(_git_path().name)
        exe.chmod(0o700)
        with chdir(self.root), patch.dict(os.environ, {"PATH": "."}):
            self.assertEqual(self.rules(path), {})

        class Deadline(Exception):
            pass

        def expired():
            raise Deadline

        with self.assertRaises(Deadline):
            git_rules([path], self.root, expired)
