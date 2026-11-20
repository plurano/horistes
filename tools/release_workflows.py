# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Bounded workflow parsing for release-please detection."""

import re

# Shared matrix bound for the sweep writer and the artifact reader (ADR-0098).
MAX_RELEASES = 256

KNOWN_RELEASE_WORKFLOWS = (
    "jellysin/release-helper/.github/workflows/release-please.yml@",
    "jellysin/release-helper/.github/workflows/release-please.yaml@",
    "plurano/horistes/.github/workflows/release-please.yml@",
    "plurano/horistes/.github/workflows/release-please.yaml@",
)
ALLOWED_TRIGGERS = ("push", "workflow_dispatch", "schedule", "workflow_run")
LOCAL_WORKFLOW_PREFIX = "./.github/workflows/"
ACTION_STEP_RE = re.compile(r"^(?:-\s+)?uses:\s*[\"']?googleapis/release-please-action@")
BLOCK_SCALAR_RE = re.compile(r":\s*[|>][-+0-9]*(?:\s*#.*)?$")


def check_hold_labels(raw, error):
    """Preserve hold labels only when bounded and safe for a single output line."""
    if len(raw) > 4096 or "\n" in raw or "\r" in raw:
        raise error("hold_labels must be a single line of at most 4096 characters")
    return raw


def is_literal_false(val):
    """Whether an if: condition is a literal false (quoted, expression, or raw)."""
    val = re.sub(r"\s+#.*$", "", val).strip()
    for _ in range(2):
        if (val.startswith("'") and val.endswith("'")) or (
            val.startswith('"') and val.endswith('"')
        ):
            val = val[1:-1].strip()
    if val.startswith("$" + "{{") and val.endswith("}}"):
        val = val[3:-2].strip()
        for _ in range(2):
            if (val.startswith("'") and val.endswith("'")) or (
                val.startswith('"') and val.endswith('"')
            ):
                val = val[1:-1].strip()
    return val.lower() == "false"


def is_custom_config_val(val, default):
    """Whether val is a non-default config path, resolving literal expressions."""
    expr_open = "$" + "{{"
    if expr_open not in val:
        return val != default
    stripped = val.strip()
    if not (stripped.startswith(expr_open) and stripped.endswith("}}")):
        return True
    inner = stripped[len(expr_open) : -2].strip()
    lit = re.match(r"^['\"](.+)['\"]$", inner)
    if lit:
        return lit.group(1) != default
    return False


def has_custom_config(lines):
    """Whether with: sets config or manifest path away from default."""
    defaults = {
        "config-file": "release-please-config.json",
        "manifest-file": ".release-please-manifest.json",
    }
    pending_key = None
    for line in lines[:100]:
        clean = re.sub(r"\s+#.*$", "", line).strip()
        if pending_key is not None:
            key, pending_key = pending_key, None
            if is_custom_config_val(clean.strip("'\""), defaults[key]):
                return True
            continue
        items = (
            clean.strip("{}").split(",")
            if (clean.startswith("{") and clean.endswith("}"))
            else [clean]
        )
        for item in items[:10]:
            match = re.match(r"^([a-zA-Z0-9_-]+)\s*:\s*(.*)$", item.strip())
            if not match:
                continue
            key = match.group(1).replace("_", "-")
            val = match.group(2).strip().strip("'\"")
            if key not in defaults:
                continue
            if val in (">", ">-", "|", "|-"):
                pending_key = key
                continue
            if is_custom_config_val(val, defaults[key]):
                return True
    return False


def inline_triggers(rest):
    """Event names of an inline on: value: a name, a [list] or a {mapping}."""
    if not (rest[:1] in "[{" and rest[-1:] in "]}"):
        return [rest.strip("'\"")]
    ends = ":" if rest.startswith("{") else ",]"
    names = []
    depth = 0
    word = ""
    for char in rest[:2000]:
        if char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        if depth == 1 and char not in "[{]},:":
            word += char
            continue
        if depth <= 1 and char in ends and word.strip():
            names.append(word.strip().strip("'\""))
        if depth <= 1 and char in ",:]}[{":
            word = ""
    return names


def push_lines(events, push_indent, truncated):
    """Push children; None when the window cuts them."""
    for position, (indent, _) in enumerate(events[:5000]):
        if indent <= push_indent:
            return events[:position]
    return None if truncated else events


def literal_pattern(value, key):
    """A literal name or star pattern, else None."""
    value = value.strip()
    if value.startswith(("&", "*", "!!")):
        return None
    if value[:1] in ("'", '"') and value[-1:] == value[:1]:
        value = value[1:-1]
    pattern = value.removeprefix("!") if key == "branches" else value
    if pattern.startswith("refs/") or not re.fullmatch(r"[A-Za-z0-9._/*-]+", pattern):
        return None
    return value


def sequence_values(following, indent):
    """Block sequence items up to the next filter."""
    values = []
    for level, clean in following[:5000]:
        if level < indent or (level == indent and not clean.startswith("- ")):
            break
        if not clean.startswith("- "):
            return None
        values.append(clean[2:])
    return values or None


def filter_patterns(value, following, indent, key):
    """Scalar, flow or block patterns, at most 200."""
    if not value:
        values = sequence_values(following, indent)
    elif value == "[]":
        values = []
    elif value.startswith("[") and value.endswith("]"):
        values = value[1:-1].split(",")
    else:
        values = [value]
    if values is None or len(values) > 200:
        return None
    patterns = [literal_pattern(v, key) for v in values[:200]]
    return None if None in patterns else patterns


def push_filters(events, indent, truncated):
    """Allow-listed push filters, else None."""
    block = push_lines(events, indent, truncated)
    if block is None:
        return None
    filters = {}
    child_indent = block[0][0] if block else 0
    sequence = False
    for index, (level, clean) in enumerate(block[:5000]):
        if level != child_indent or (sequence and clean.startswith("- ")):
            continue
        match = re.fullmatch(r"""(['"]?)([A-Za-z0-9_-]+)\1\s*:\s*(.*)""", clean)
        if not match:
            return None
        key, value = match[2], match[3]
        sequence = not value
        if key in ("paths", "paths-ignore"):
            continue
        if key not in ("branches", "branches-ignore", "tags", "tags-ignore"):
            return None
        patterns = filter_patterns(value, block[index + 1 :], child_indent, key)
        if patterns is None or key in filters:
            return None
        filters[key] = patterns
    return filters


def branch_matches(pattern, default_branch):
    """Anchored glob: * stops at a slash, ** crosses it."""
    regex = re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
    return re.fullmatch(regex, default_branch) is not None


def push_runs_on_branch(events, indent, default_branch, truncated):
    """Whether push admits the default branch; unknown filters count."""
    filters = push_filters(events, indent, truncated)
    if filters is None or {"branches", "branches-ignore"} <= filters.keys():
        return True
    if "branches" in filters:
        included = False
        for pattern in filters["branches"][:200]:
            if branch_matches(pattern.removeprefix("!"), default_branch):
                included = not pattern.startswith("!")
        return included
    if "branches-ignore" in filters:
        return not any(branch_matches(p, default_branch) for p in filters["branches-ignore"][:200])
    return not ({"tags", "tags-ignore"} & filters.keys())


def allowed_event(clean, following, indent, default_branch, truncated):
    """Whether one event entry admits the default branch."""
    key_m = re.match(r"^([a-zA-Z0-9_-]+):\s*(.*)", clean)
    if key_m:
        name, value = key_m.groups()
        if name == "push" and not value:
            return push_runs_on_branch(following, indent, default_branch, truncated)
        return name in ALLOWED_TRIGGERS
    seq_m = re.match(r"^-\s*([a-zA-Z0-9_-]+)", clean)
    return bool(seq_m and seq_m.group(1) in ALLOWED_TRIGGERS)


def trigger_lines(lines):
    """Event candidates outside comments and block scalars."""
    block_indent = None
    for line in lines[:5000]:
        clean = re.sub(r"\s+#.*$", "", line.strip()).strip()
        if not clean:
            continue
        indent = len(line) - len(line.lstrip(" "))
        if block_indent is not None and indent > block_indent:
            continue
        block_indent = None
        if clean.startswith("#"):
            continue
        if BLOCK_SCALAR_RE.search(clean):
            block_indent = indent
        yield indent, clean


def block_has_allowed_trigger(events, default_branch, truncated):
    """Direct event keys until the next top-level key."""
    on_indent = events[0][0] if events else 0
    for position, (indent, clean) in enumerate(events[:5000]):
        if indent == 0:
            return False
        if indent == on_indent and allowed_event(
            clean, events[position + 1 :], indent, default_branch, truncated
        ):
            return True
    return truncated


def workflow_has_allowed_trigger(content, default_branch):
    """Whether top-level on: admits the default branch."""
    lines = content.splitlines()
    events = list(trigger_lines(lines))
    for position, (indent, clean) in enumerate(events[:5000]):
        if indent != 0 or not re.match(r"^on:\s*(.*)$", clean):
            continue
        rest = clean.split(":", 1)[1].strip()
        if rest:
            return any(t in ALLOWED_TRIGGERS for t in inline_triggers(rest))
        return block_has_allowed_trigger(events[position + 1 :], default_branch, len(lines) > 5000)
    return len(lines) > 5000


def job_direct_indent(job_lines):
    """Indent of a job's own keys: its first line that is neither blank nor a comment."""
    for line in job_lines[:1000]:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return len(line) - len(line.lstrip(" "))
    return None


def job_key(clean, own_level, flags):
    """Record what one job line sets in flags; True when the line opens a with: block."""
    if own_level and clean.startswith("if:"):
        if is_literal_false(clean.split(":", 1)[1]):
            flags["dormant"] = True
    elif own_level and clean.startswith("uses:"):
        target = clean.split(":", 1)[1].strip().strip("'\"")
        if any(target.startswith(w) for w in KNOWN_RELEASE_WORKFLOWS):
            flags["reusable"] = True
        if target.removeprefix(LOCAL_WORKFLOW_PREFIX) in flags["local"]:
            flags["reusable"] = True
    elif own_level and clean.startswith("with:"):
        rest = clean.split(":", 1)[1].strip()
        if not rest:
            return True
        flags["custom"] = flags["custom"] or has_custom_config([rest])
    if ACTION_STEP_RE.search(clean):
        flags["action"] = True
    return False


def job_runs_release(job_lines, local):
    """Whether a single job runs release-please action or reusable workflow."""
    direct_indent = job_direct_indent(job_lines)
    if direct_indent is None:
        return False
    flags = {"dormant": False, "reusable": False, "action": False, "custom": False}
    flags["local"] = local
    block_indent = None
    in_with = False
    with_lines = []
    for line in job_lines[:1000]:
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if not stripped:
            continue
        if block_indent is not None and indent > block_indent:
            if in_with:
                with_lines.append(line)
            continue
        block_indent = None
        if stripped.startswith("#"):
            continue
        if BLOCK_SCALAR_RE.search(line):
            block_indent = indent
        if in_with and indent > direct_indent:
            with_lines.append(line)
            continue
        in_with = False
        clean = re.sub(r"\s+#.*$", "", stripped)
        if job_key(clean, indent == direct_indent, flags):
            in_with = True
    custom_config = flags["custom"] or has_custom_config(with_lines)
    reusable_call = flags["reusable"]
    if flags["dormant"]:
        return False
    if flags["action"]:
        return True
    return bool(reusable_call and not custom_config)


def workflow_runs_release(content, local, default_branch):
    """Whether workflow triggers and jobs structure run release-please."""
    if not workflow_has_allowed_trigger(content, default_branch):
        return False
    return jobs_run_release(content, local)


def jobs_run_release(content, local):
    """Whether a job of the workflow runs release-please, triggers aside.

    local names the workflow files of this repository that run it themselves,
    so a job-level call of ./.github/workflows/<name> counts too.
    """
    in_jobs = False
    job_indent = None
    current_job = []
    lines = content.splitlines()[:5000]
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current_job:
                current_job.append(line)
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            if in_jobs:
                if current_job and job_runs_release(current_job, local):
                    return True
                current_job = []
                in_jobs = False
            if re.match(r"^jobs:\s*(?:#.*)?$", stripped):
                in_jobs = True
                job_indent = None
            continue
        if not in_jobs:
            continue
        if stripped.startswith("#"):
            if current_job:
                current_job.append(line)
            continue
        if job_indent is None:
            job_indent = indent
        if indent == job_indent and re.match(r"^[a-zA-Z0-9_-]+:\s*(?:#.*)?$", stripped):
            if current_job and job_runs_release(current_job, local):
                return True
            current_job = []
        elif current_job is not None:
            current_job.append(line)
    return bool(in_jobs and current_job and job_runs_release(current_job, local))


def local_release_workflows(contents):
    """Local workflow files that run release-please, directly or through local calls.

    Grows the set level by level; GitHub connects at most ten levels of workflows
    and permits no loops, so ten rounds reach every chain and a cycle cannot spin.
    """
    local = frozenset()
    for _ in range(10):
        grown = frozenset(n for n, c in contents.items() if jobs_run_release(c, local))
        if grown == local:
            break
        local = grown
    return local
