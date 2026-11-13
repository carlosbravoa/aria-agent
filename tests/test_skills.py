"""
Skills: capped prompt index, on-demand load (paged, fenced as a procedure),
self-authored save/delete and their guard rails.
"""

from __future__ import annotations

import os

import pytest


def _install(home_ws, name, desc="Do the thing", body="Step 1\nStep 2", single=False,
             extra=None):
    """Write a user-installed skill under the isolated ~/.aria/skills."""
    from aria import config
    root = config.skills_dir()
    root.mkdir(parents=True, exist_ok=True)
    text = f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}\n"
    if single:
        (root / f"{name}.md").write_text(text)
    else:
        (root / name).mkdir(exist_ok=True)
        (root / name / "SKILL.md").write_text(text)
        for rel, content in (extra or {}).items():
            p = root / name / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
    return root


# ── Discovery / parsing ──────────────────────────────────────────────────────

def test_discover_both_layouts_and_skips_junk(minimal_env):
    from aria import skills
    root = _install(minimal_env, "gh-release", "Cut a release")
    _install(minimal_env, "deploy", "Deploy staging", single=True)
    (root / "Bad Name").mkdir()
    (root / "Bad Name" / "SKILL.md").write_text("---\ndescription: x\n---\nb")
    (root / "empty-dir").mkdir()
    (root / "notes.txt").write_text("not a skill")
    assert [s.name for s in skills.discover()] == ["deploy", "gh-release"]


def test_frontmatter_folded_and_quoted_description(minimal_env):
    from aria import config, skills
    root = config.skills_dir()
    (root / "a").mkdir(parents=True)
    (root / "a" / "SKILL.md").write_text(
        "---\nname: a\ndescription: >\n  Use when\n  releasing\n---\nbody")
    (root / "b.md").write_text('---\ndescription: "Quoted: yes"\n---\nbody')
    (root / "c.md").write_text("# Title\n\nFirst real line.\nmore")
    got = {s.name: s.description for s in skills.discover()}
    assert got == {"a": "Use when releasing", "b": "Quoted: yes",
                   "c": "First real line."}


# ── Prompt index (the standing token cost) ───────────────────────────────────

def test_index_empty_without_skills(minimal_env):
    from aria import skills
    assert skills.index_block() == ""


def test_index_lists_names_not_bodies(minimal_env):
    from aria import skills
    _install(minimal_env, "gh-release", "Cut a release", body="SECRET-BODY-TEXT")
    block = skills.index_block()
    assert "- gh-release: Cut a release" in block
    assert "SECRET-BODY-TEXT" not in block


def test_index_capped_count_and_description(minimal_env, monkeypatch):
    from aria import skills
    for i in range(5):
        _install(minimal_env, f"s{i}", "d" * 300)
    # s0 is the stalest → dropped first when over the cap.
    old = skills.get("s0").path
    os.utime(old, (1, 1))
    monkeypatch.setenv("ARIA_SKILLS_MAX", "3")
    monkeypatch.setenv("ARIA_SKILL_DESC_CHARS", "40")
    block = skills.index_block()
    listed = [ln for ln in block.splitlines() if ln.startswith("- s")]
    assert len(listed) == 3 and not any(ln.startswith("- s0:") for ln in listed)
    assert all(len(ln) <= len("- sN: ") + 40 for ln in listed)
    assert "+2 more" in block


def test_index_disabled_with_zero(minimal_env, monkeypatch):
    from aria import skills
    _install(minimal_env, "x", "y")
    monkeypatch.setenv("ARIA_SKILLS_MAX", "0")
    assert skills.index_block() == ""


def test_system_prompt_carries_index_only(minimal_env):
    from aria.agent import Agent
    _install(minimal_env, "gh-release", "Cut a release", body="BODY-NOT-IN-PROMPT")
    a = Agent()
    assert "## Skills" in a.system_prompt
    assert "- gh-release: Cut a release" in a.system_prompt
    assert "BODY-NOT-IN-PROMPT" not in a.system_prompt


def test_system_prompt_has_no_skills_section_when_none(minimal_env):
    from aria.agent import Agent
    assert "## Skills" not in Agent().system_prompt


# ── Load ─────────────────────────────────────────────────────────────────────

def test_load_returns_body_without_frontmatter(minimal_env):
    from aria.tools import skill
    _install(minimal_env, "gh-release", body="Run gh release create")
    out = skill.execute({"action": "load", "name": "gh-release"})
    assert out.startswith("[skill loaded] gh-release")
    assert "Run gh release create" in out and "description:" not in out


def test_load_pages_long_bodies_under_tool_truncation(minimal_env, monkeypatch):
    from aria import skills
    monkeypatch.setenv("ARIA_SKILL_MAX_CHARS", "1000")
    body = "".join(f"line {i:04d}\n" for i in range(300))     # 2700 chars
    _install(minimal_env, "long", body=body)
    first = skills.load("long")
    assert "offset=1000" in first and len(first) < 6000
    second = skills.load("long", offset=1000)
    assert "chars 1000–2000" in second
    rebuilt = "".join(
        skills.load("long", offset=o).split("\n", 1)[1].split("\n\n[continues")[0]
        for o in (0, 1000, 2000))
    assert rebuilt.strip() == body.strip()


def test_body_cap_is_clamped_below_tool_truncation(minimal_env, monkeypatch):
    from aria import skills
    monkeypatch.setenv("ARIA_SKILL_MAX_CHARS", "999999")
    assert skills.body_chars() <= 5500


def test_load_supporting_file_and_blocks_traversal(minimal_env):
    from aria.tools import skill
    root = _install(minimal_env, "gh-release", extra={"ref/notes.md": "EXTRA"})
    (root / "outside.md").write_text("---\ndescription: o\n---\nOUTSIDE")
    out = skill.execute({"action": "load", "name": "gh-release"})
    assert "ref/notes.md" in out
    assert "EXTRA" in skill.execute(
        {"action": "load", "name": "gh-release", "file": "ref/notes.md"})
    for bad in ("../outside.md", "/etc/passwd", "../../.env"):
        res = skill.execute({"action": "load", "name": "gh-release", "file": bad})
        assert res.startswith("[skill] error"), bad


@pytest.mark.parametrize("name", ["../x", "a/b", "", ".hidden", "UP CASE"])
def test_load_rejects_bad_names(minimal_env, name):
    from aria.tools import skill
    assert skill.execute({"action": "load", "name": name}).startswith("[skill] error")


def test_list_and_query(minimal_env):
    from aria.tools import skill
    assert "No skills installed" in skill.execute({"action": "list"})
    _install(minimal_env, "gh-release", "Cut a GitHub release")
    _install(minimal_env, "deploy", "Deploy staging")
    assert "gh-release" in skill.execute({"action": "list", "query": "github"})
    assert "deploy" not in skill.execute({"action": "list", "query": "github"})
    assert "No skill matches" in skill.execute({"action": "list", "query": "zzz"})


# ── Fencing in the loop ──────────────────────────────────────────────────────

def test_loaded_skill_is_fenced_as_procedure_errors_as_data(minimal_env, native_client):
    from aria import skills
    from aria.agent import _UNTRUSTED_OPEN, Agent
    _install(minimal_env, "gh-release", body="Run gh release create")
    a = Agent()
    a.client = native_client(
        {"content": None, "tool_calls": [("skill", {"action": "load", "name": "gh-release"})]},
        {"content": None, "tool_calls": [("skill", {"action": "load", "name": "nope"})]},
        "done")
    a.chat_collect("release it")
    tool_msgs = [m["content"] for m in a.history if m.get("role") == "tool"]
    assert tool_msgs[0].startswith(skills.SKILL_OPEN)
    assert _UNTRUSTED_OPEN not in tool_msgs[0]
    assert tool_msgs[1].startswith(_UNTRUSTED_OPEN)


def test_self_written_skill_gets_softer_fence(minimal_env):
    from aria import skills
    skills.save("mine", "When testing", "do x")
    fenced = skills.fence(skills.load("mine"))
    assert fenced.startswith(skills.SKILL_OPEN_SELF)
    assert skills.fence("[skill] error: nope") is None


# ── Save / delete ────────────────────────────────────────────────────────────

def test_save_roundtrip_private_and_marked(minimal_env):
    from aria import skills
    from aria.tools import skill
    out = skill.execute({"action": "save", "name": "gh-release",
                         "description": "Cut a release", "body": "1. gh release create"})
    assert "Saved skill" in out
    s = skills.get("gh-release")
    assert s.source == skills.SELF and s.description == "Cut a release"
    assert (s.path.stat().st_mode & 0o777) == 0o600
    assert "gh release create" in skills.load("gh-release")
    # Replacing its own skill is allowed.
    skills.save("gh-release", "Cut a release v2", "new body")
    assert "new body" in skills.load("gh-release")


def test_save_redacts_secrets(minimal_env):
    from aria import skills
    skills.save("api", "Call the API",
                "curl https://user:hunter2pass@example.com/x")
    assert "hunter2pass" not in skills.get("api").path.read_text()


@pytest.mark.parametrize("args, needle", [
    ({"name": "Bad Name", "description": "d", "body": "b"}, "name must be"),
    ({"name": "ok", "description": "", "body": "b"}, "description"),
    ({"name": "ok", "description": "d", "body": ""}, "empty body"),
    ({"name": "ok", "description": "d" * 400, "body": "b"}, "description is"),
    ({"name": "ok", "description": "d", "body": "x" * 6000}, "cap is"),
])
def test_save_refusals(minimal_env, args, needle):
    from aria.tools import skill
    out = skill.execute({"action": "save", **args})
    assert out.startswith("[skill] error") and needle in out


def test_save_never_overwrites_installed_skill(minimal_env):
    from aria import skills
    from aria.tools import skill
    _install(minimal_env, "deploy", body="USER BODY")
    out = skill.execute({"action": "save", "name": "deploy",
                         "description": "d", "body": "hijack"})
    assert "user installed" in out
    assert "USER BODY" in skills.load("deploy")


def test_save_and_delete_refused_in_task_runs(minimal_env, monkeypatch):
    from aria import skills
    from aria.tools import skill
    skills.save("mine", "d", "b")
    monkeypatch.setenv("ARIA_TASK_ID", "t1")
    assert "unattended" in skill.execute(
        {"action": "save", "name": "x", "description": "d", "body": "b"})
    assert "unattended" in skill.execute({"action": "delete", "name": "mine"})
    assert skills.get("mine")


def test_delete_only_self_written(minimal_env):
    from aria import skills
    from aria.tools import skill
    _install(minimal_env, "deploy")
    skills.save("mine", "d", "b")
    assert "user installed" in skill.execute({"action": "delete", "name": "deploy"})
    assert "Deleted" in skill.execute({"action": "delete", "name": "mine"})
    assert [s.name for s in skills.discover()] == ["deploy"]


def test_skill_schema_stays_small():
    """The schema is sent on every request — keep it terse."""
    import json

    from aria.tools import skill
    assert len(json.dumps(skill.DEFINITION)) < 1500


# ── Skill directory (bundled scripts) ────────────────────────────────────────

def test_load_exposes_skill_directory_and_substitutes_variable(minimal_env):
    from aria import config, skills
    _install(minimal_env, "pdf", body="Run `python ${CLAUDE_SKILL_DIR}/scripts/x.py` "
                                      "or $CLAUDE_SKILL_DIR/scripts/y.sh",
             extra={"scripts/x.py": "print(1)"})
    home = str(config.skills_dir() / "pdf")
    out = skills.load("pdf")
    assert f"python {home}/scripts/x.py" in out and f"{home}/scripts/y.sh" in out
    assert "CLAUDE_SKILL_DIR" not in out
    assert f"under {home}: scripts/x.py" in out


def test_load_without_supporting_files_adds_no_directory_line(minimal_env):
    from aria import skills
    _install(minimal_env, "plain", body="just text")
    assert "supporting files" not in skills.load("plain")


# ── Import ───────────────────────────────────────────────────────────────────

def _source(tmp_path, name="pdf-tools", desc="Work with PDFs", body="1. Run pdftotext",
            extra=None, front=""):
    src = tmp_path / "src" / name
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n{front}---\n\n{body}\n")
    for rel, content in (extra or {}).items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return src


class _LLM:
    """Scripted model: replies in order, records prompts."""
    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def llm():
    from aria import skills
    tokens = []

    def bind(*replies):
        fake = _LLM(*replies)
        tokens.append(skills.set_llm(fake))
        return fake
    yield bind
    for t in reversed(tokens):
        skills.reset_llm(t)


def test_import_compatible_skill_makes_no_model_call(minimal_env, tmp_path, llm):
    from aria import skills
    fake = llm()
    src = _source(tmp_path, extra={"scripts/run.py": "print(1)", ".git/x": "no"},
                  front="allowed-tools: Bash\n")
    report = skills.import_skill(src)
    assert fake.prompts == []
    assert "No adaptation needed" in report and "allowed-tools" in report
    s = skills.get("pdf-tools")
    assert s.source == skills.IMPORTED and s.description == "Work with PDFs"
    assert (s.root / "scripts" / "run.py").read_text() == "print(1)"
    assert not (s.root / ".git").exists()
    assert "allowed-tools" not in s.path.read_text()
    assert skills.fence(skills.load("pdf-tools")).startswith(skills.SKILL_OPEN)


def test_import_single_md_file_and_name_normalised(minimal_env, tmp_path, llm):
    from aria import skills
    llm()
    f = tmp_path / "My Deploy Notes.md"
    f.write_text("---\ndescription: Deploy it\n---\nrun make deploy")
    skills.import_skill(f)
    assert skills.get("my-deploy-notes").description == "Deploy it"


def test_import_long_description_is_shortened_by_model(minimal_env, tmp_path, llm):
    from aria import skills
    fake = llm('"Extract text and tables from PDFs; use for any .pdf task"')
    src = _source(tmp_path, desc="x" * 600)
    report = skills.import_skill(src)
    assert "description shortened 600 →" in report
    assert len(fake.prompts) == 1 and "at most 120 characters" in fake.prompts[0]
    assert skills.get("pdf-tools").description == (
        "Extract text and tables from PDFs; use for any .pdf task")


def test_import_description_retry_then_no_way(minimal_env, tmp_path, llm):
    from aria import config, skills
    from aria.tools import skill
    fake = llm("y" * 300, "z" * 300)
    src = _source(tmp_path, desc="x" * 600)
    out = skill.execute({"action": "import", "path": str(src)})
    assert out.startswith("[skill] error: no way") and "description" in out
    assert len(fake.prompts) == 2
    assert skills.discover() == []                       # nothing half-written
    assert not list(config.skills_dir().glob(".*importing"))


def test_import_missing_description_is_written_by_model(minimal_env, tmp_path, llm):
    from aria import skills
    llm("Convert PDFs to text")
    f = tmp_path / "pdf.md"
    f.write_text("---\nname: pdf\n---\nrun pdftotext")
    assert "description written" in skills.import_skill(f)
    assert skills.get("pdf").description == "Convert PDFs to text"


def test_import_adapts_claude_specific_body(minimal_env, tmp_path, llm):
    from aria import skills
    body = ("Use the Read tool to open the file named in $ARGUMENTS.\n"
            "Then run `pdftotext` via the Bash tool and report the result.\n") * 3
    adapted = body.replace("the Read tool", "file_access").replace(
        "$ARGUMENTS", "the user's request").replace("the Bash tool", "shell_run")
    fake = llm("```markdown\n" + adapted + "\n```")
    report = skills.import_skill(_source(tmp_path, body=body))
    assert "instructions rewritten" in report and "$ARGUMENTS" in report
    assert "file_access" in fake.prompts[0] and "IMPOSSIBLE" in fake.prompts[0]
    loaded = skills.load("pdf-tools")
    assert "shell_run" in loaded and "$ARGUMENTS" not in loaded and "```markdown" not in loaded


@pytest.mark.parametrize("reply, needle", [
    ("IMPOSSIBLE: needs subagents to fan out", "needs subagents"),
    ("short summary", "didn't preserve"),
    (None, "still relies on"),                         # echoes the body unchanged
    ("", "no usable answer"),
    (RuntimeError("connection refused"), "connection refused"),
])
def test_import_body_no_way(minimal_env, tmp_path, llm, reply, needle):
    from aria import skills
    from aria.tools import skill
    body = "Spawn a subagent per file, passing $ARGUMENTS to each one.\n" * 4
    llm(body if reply is None else reply)
    out = skill.execute({"action": "import", "path": str(_source(tmp_path, body=body))})
    assert out.startswith("[skill] error: no way") and needle in out
    assert skills.discover() == []


def test_import_needing_adaptation_without_model_is_no_way(minimal_env, tmp_path):
    from aria.tools import skill
    out = skill.execute({"action": "import",
                         "path": str(_source(tmp_path, desc="x" * 600))})
    assert out.startswith("[skill] error: no way") and "no model" in out


def test_import_guards(minimal_env, tmp_path, llm, monkeypatch):
    from aria import skills
    from aria.tools import skill
    llm()
    # not a skill
    (tmp_path / "empty").mkdir()
    assert "no way" in skill.execute({"action": "import", "path": str(tmp_path / "empty")})
    assert "no way" in skill.execute({"action": "import", "path": str(tmp_path / "nope")})
    assert "path is required" in skill.execute({"action": "import"})
    # protected source
    assert "protected path" in skill.execute({"action": "import", "path": "~/.ssh"})
    # never replaces a hand-installed or self-written skill; re-import is fine
    src = _source(tmp_path)
    _install(minimal_env, "pdf-tools", body="USER BODY")
    assert "already exists" in skill.execute({"action": "import", "path": str(src)})
    assert "Imported" in skill.execute(
        {"action": "import", "path": str(src), "name": "pdf2"})
    assert "Imported" in skill.execute(
        {"action": "import", "path": str(src), "name": "pdf2"})
    assert "USER BODY" in skills.load("pdf-tools")
    # unattended
    monkeypatch.setenv("ARIA_TASK_ID", "t1")
    assert "unattended" in skill.execute(
        {"action": "import", "path": str(src), "name": "pdf3"})


def test_import_too_many_files_is_no_way(minimal_env, tmp_path, llm, monkeypatch):
    from aria import skills
    llm()
    monkeypatch.setattr(skills, "_MAX_FILES", 2)
    src = _source(tmp_path, extra={f"f{i}.txt": "x" for i in range(4)})
    with pytest.raises(skills.SkillError, match="bundles too much"):
        skills.import_skill(src)


def test_import_asks_for_approval_on_channel_turns(minimal_env, tmp_path, monkeypatch):
    from aria import approval, skills
    from aria.tools import skill
    assert approval.required("skill_import")
    monkeypatch.setattr(approval, "should_ask", lambda: True)
    asked = []
    monkeypatch.setattr(approval, "request",
                        lambda summary, kind=None: (asked.append(summary) or (False, "denied")))
    out = skill.execute({"action": "import", "path": str(_source(tmp_path))})
    assert out.startswith("[approval] Not done") and asked
    assert skills.discover() == []


def test_agent_lends_active_model_to_import(minimal_env, tmp_path, native_client):
    """End to end through the loop: the import's adaptation call goes to the
    agent's own client (same endpoint/profile), not a separate one."""
    from aria import skills
    from aria.agent import Agent
    src = _source(tmp_path, desc="x" * 600)
    a = Agent()
    a.client = native_client(
        {"content": None, "tool_calls": [("skill", {"action": "import", "path": str(src)})]},
        "Handle PDF files",                 # ← the side call made by the import
        "Imported.")
    a.chat_collect(f"import the skill at {src}")
    assert skills.get("pdf-tools").description == "Handle PDF files"
    assert skills._llm.get() is None        # binding doesn't leak past the call
