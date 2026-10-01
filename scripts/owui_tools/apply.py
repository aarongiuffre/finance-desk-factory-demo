#!/usr/bin/env python3
"""apply.py -- upsert the mirror tool sources into Open WebUI's tool table.
Run INSIDE the container (webui.db is root-owned):
  docker exec open-webui python /data/finance/scripts/owui_tools/apply.py

PITFALL (found 2026-09-26): OWUI validates tool.specs against pydantic
FunctionCallConfig, which is a LIST of function schemas. A tool row with
specs='{}' raises ValidationError inside /api/v1/tools listing and kills the
ENTIRE tools list in the UI -- one bad row hides every tool. apply.py now
derives specs from the tool module's function signature (descriptions from
the docstring) and always writes a valid list, and repairs any other row
whose specs aren't a list.
"""
import inspect
import json
import os
import re
import sqlite3
import sys
import time
import types

DB = "/app/backend/data/webui.db"
HERE = "/data/finance/scripts/owui_tools"
TOOLS = ["search_filings", "get_quote", "read_filing", "list_filings",
         "screen_fundamentals", "price_technicals", "confluence",
         "backtest_report"]

PY2JSON = {str: "string", int: "integer", float: "number", bool: "boolean"}


def derive_specs(mod):
    """Build the specs list from the module's main callable: the top-level
    function named like the module (fallback: first public function)."""
    modname = mod.__name__.rsplit(".", 1)[-1]
    # the OWUI frontmatter 'title:' is the registered function name (may differ from file);
    # snake_case titles match the method exactly, Title Case titles match loosely (norm = lower, alnum only)
    m = re.search(r"^title:\s*(.+)$", open(f"{HERE}/{modname}.py").read(), re.M)
    title = (m.group(1).strip() if m else modname)
    norm = lambda x: re.sub(r"[^a-z0-9]", "", x.lower())  # noqa: E731
    snake = lambda x: re.sub(r"[^a-z0-9]+", "_", x.lower()).strip("_")  # noqa: E731
    fname = None
    tools_cls = getattr(mod, "Tools", None)
    if tools_cls is not None:  # OWUI style: class Tools with method(s)
        meths = [v for k, v in vars(tools_cls).items() if isinstance(v, types.FunctionType)]
        fn = next((x for x in meths if x.__name__ == modname), None) \
            or next((x for x in meths if x.__name__ == snake(title)), None) \
            or next((x for x in meths if norm(x.__name__) == norm(title)), None) \
            or (meths[0] if len(meths) == 1 else None)
        if len(meths) > 1:
            # multi-function tool (e.g. summary+events): emit ALL methods as
            # separate function schemas — OWUI registers each one
            return [_one(m) for m in meths]
        # registered function name = the matched method name (what OWUI actually
        # invokes); fall back to snake_case(title)
        fname = fn.__name__ if fn is not None else snake(title)
    else:
        fname = title
    if tools_cls is None:
        cands = [v for k, v in vars(mod).items()
                 if isinstance(v, types.FunctionType) and v.__module__ == mod.__name__
                 and not k.startswith("_")]
        fn = next((f for f in cands if f.__name__ == fname), cands[0] if cands else None)
    if fn is None:
        raise RuntimeError(f"{mod.__name__}: no function to derive specs from")
    return [_one(fn)]


def _one(fn):
    """Schema for one callable; registered name = method name."""
    doc = inspect.getdoc(fn) or ""
    desc = doc.strip().split("\n\n")[0].replace("\n", " ")
    pd = {k: v.strip() for k, v in re.findall(r":param\s+(\w+)[:\s]+(.+?)(?=\s*:param|\Z)", doc, re.S)}
    props, required = {}, []
    for pname, p in inspect.signature(fn).parameters.items():
        if pname in ("self", "__valves__") or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        prop = {"type": PY2JSON.get(p.annotation, "string"),
                "description": pd.get(pname, pname)}
        if p.default is inspect.Parameter.empty:
            required.append(pname)
        elif p.default is not None:
            prop["default"] = p.default
        props[pname] = prop
    return {"name": fn.__name__, "description": desc,
            "parameters": {"type": "object", "properties": props,
                           "required": required}}


def load_module(name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, f"{HERE}/{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def main():
    db = sqlite3.connect(DB)
    now = int(time.time())
    existing = {r[0]: r for r in db.execute("select id, user_id from tool")}
    admin = db.execute("select id from user where role='admin' limit 1").fetchone()[0]
    owner = (existing.get("search_filings") or (None, admin))[1] or admin
    for name in TOOLS:
        content = open(f"{HERE}/{name}.py").read()
        try:
            specs = json.dumps(derive_specs(load_module(name)))
        except Exception as e:  # noqa: BLE001
            print(f"ERROR deriving specs for {name}: {e} -- row left untouched")
            continue
        # OPTIONAL_OVERRIDES: hand-curated spec tweaks (enums) applied over derived specs
        ov_path = f"{HERE}/specs_override.json"
        if os.path.isfile(ov_path):
            ov = json.load(open(ov_path))
            if name in ov:
                specs_list = json.loads(specs)
                for fn_spec in specs_list:
                    props = fn_spec["parameters"]["properties"]
                    for fn_key, param_map in ov[name].items():
                        if fn_key not in (fn_spec["name"], name):
                            continue
                        for pk, pv in param_map.items():
                            if pk in props:
                                props[pk].update(pv)
                specs = json.dumps(specs_list)
        if name in existing:
            db.execute("update tool set content=?, specs=?, updated_at=? where id=?",
                       (content, specs, now, name))
            print("updated", name)
        else:
            db.execute(
                "insert into tool (id,user_id,name,content,specs,meta,valves,updated_at,created_at)"
                " values (?,?,?,?,?,?,?,?,?)",
                (name, owner, name.replace("_", " ").title(), content, specs, "{}", "{}", now, now),
            )
            print("created", name)
    # repair ANY row whose specs aren't a valid list (protects whole listing)
    for tid, sp in db.execute("select id, specs from tool").fetchall():
        try:
            ok = isinstance(json.loads(sp), list)
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            db.execute("update tool set specs='[]' where id=?", (tid,))
            print(f"repaired non-list specs on '{tid}' -> [] (re-derive manually if it matters)")
    db.commit()
    bad = []
    for tid, sp in db.execute("select id, specs from tool").fetchall():
        try:
            if not isinstance(json.loads(sp), list):
                bad.append(tid)
        except Exception:  # noqa: BLE001
            bad.append(tid)
    print("audit:", "all specs valid lists" if not bad else f"STILL BAD: {bad}")
    print("done")


if __name__ == "__main__":
    main()
