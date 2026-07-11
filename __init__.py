# find_leaks.py — Binary Ninja plugin to audit leaked BinaryView refs
# Works from Tools → Leak Audit menu. Filters out refs from InteractiveConsole,
# InterpreterThread, Completer, and traceback-ish objects.

import gc
import sys
import types
import tempfile
from pathlib import Path

# Optional modules (may not exist depending on BN env)
try:
    import code as _code_mod
except Exception:
    _code_mod = None

try:
    import rlcompleter as _rlc_mod
except Exception:
    _rlc_mod = None

try:
    import threading as _threading
except Exception:
    _threading = None

try:
    import traceback as _tb_mod
except Exception:
    _tb_mod = None

import binaryninja as bn
from binaryninja import log_info, log_error
from binaryninjaui import UIAction, UIActionHandler, Menu
from binaryninja.interaction import (
    get_text_line_input,
    show_message_box,
    show_html_report,
    MessageBoxButtonSet,
    MessageBoxIcon,
)
try:
    from binaryninja import UIAction, UIActionHandler, Menu
    UI_AVAILABLE = True
except ImportError:
    UI_AVAILABLE = False

# ---------- Config ----------
DEFAULT_MAX_DEPTH = 3
DEFAULT_PER_NODE_LIMIT = 20
SHOW_REFCOUNTS = True
# ----------------------------

def _typename(o):
    try:
        return f"{o.__class__.__module__}.{o.__class__.__name__}"
    except Exception:
        return type(o).__name__

def _is_interpreter_thread(obj):
    if _threading and isinstance(obj, _threading.Thread):
        cname = obj.__class__.__name__
        if "InterpreterThread" in cname or "Interpreter" in cname:
            return True
        if isinstance(getattr(obj, "name", None), str) and "Interpreter" in obj.name:
            return True
    return "InterpreterThread" in _typename(obj)

def _is_interactive_console(obj):
    if _code_mod:
        icls = getattr(_code_mod, "InteractiveConsole", ())
        try:
            if isinstance(obj, icls):
                return True
        except Exception:
            pass
    return "code.InteractiveConsole" in _typename(obj)

def _is_completer(obj):
    if _rlc_mod:
        ccls = getattr(_rlc_mod, "Completer", ())
        try:
            if isinstance(obj, ccls):
                return True
        except Exception:
            pass
    t = _typename(obj)
    return "rlcompleter.Completer" in t or t.endswith(".Completer")

def _is_tracebackish(obj):
    if isinstance(obj, types.TracebackType):
        return True
    if isinstance(obj, types.FrameType):
        return True
    tname = _typename(obj)
    if tname.startswith("traceback.") or "TracebackException" in tname or "StackSummary" in tname:
        return True
    if isinstance(obj, dict):
        modname = obj.get("__name__", None)
        if modname == "traceback":
            return True
    return False

def _is_console_noise(ref):
    if _is_tracebackish(ref):
        return True
    if _is_interactive_console(ref):
        return True
    if _is_completer(ref):
        return True
    if _is_interpreter_thread(ref):
        return True

    # Heuristic to avoid our own temporary containers
    if isinstance(ref, (list, tuple, set, frozenset)):
        r = repr(ref)
        if "live_bvs" in r or "print_referrers" in r or "inspect_bv" in r:
            return True

    if isinstance(ref, dict):
        modname = ref.get("__name__", "")
        if modname in ("__main__", "__console__", "code", "rlcompleter"):
            return True

    return False

def live_bvs():
    """Return a list of all BinaryView instances still reachable from Python."""
    gc.collect()
    return [o for o in gc.get_objects() if isinstance(o, bn.binaryview.BinaryView)]

def _describe_bv(bv):
    try:
        fname = getattr(getattr(bv, "file", None), "filename", None)
        short = str(bv)
        if fname:
            return f"{short} (file='{fname}')"
        return short
    except Exception:
        return repr(bv)

def list_bvs(show_referrers_count=True):
    """Print all live BVs with optional refcount and count of filtered referrers."""
    bvs = live_bvs()
    if not bvs:
        log_info("No live BinaryView objects found.")
        return bvs

    log_info(f"Found {len(bvs)} BinaryView object(s):")
    for i, bv in enumerate(bvs):
        pieces = [f"[{i}] {_describe_bv(bv)}"]
        if SHOW_REFCOUNTS:
            try:
                pieces.append(f"refcnt={sys.getrefcount(bv)-1}")
            except Exception:
                pass
        if show_referrers_count:
            try:
                refs = [r for r in gc.get_referrers(bv) if not _is_console_noise(r)]
                pieces.append(f"interesting_referrers={len(refs)}")
            except Exception:
                pass
        log_info("  " + "  |  ".join(pieces))
    log_info("Tip: Tools → Leak Audit → Inspect BV by Index… to drill into referrers.")
    return bvs

def _safe_preview(obj, maxlen=120):
    try:
        r = repr(obj)
    except Exception:
        r = f"<unreprable {type(obj).__name__}>"
    if len(r) > maxlen:
        r = r[:maxlen-3] + "..."
    return r

def _iter_referrers_filtered(obj):
    for ref in gc.get_referrers(obj):
        if _is_console_noise(ref):
            continue
        if isinstance(ref, dict):
            for k in ("inspect_bv", "print_referrers", "_iter_referrers_filtered"):
                if k in ref:
                    break
            else:
                yield ref
        else:
            yield ref

def print_referrers(obj, max_depth=DEFAULT_MAX_DEPTH, per_node_limit=DEFAULT_PER_NODE_LIMIT):
    """Print a filtered, bounded backref tree for `obj`."""
    seen_ids = set()

    def walk(node, depth, indent=""):
        if depth < 0:
            return
        rid = id(node)
        if rid in seen_ids:
            log_info(f"{indent}↳ (cycle) {type(node).__name__} {hex(rid)}")
            return
        seen_ids.add(rid)

        try:
            refs = list(_iter_referrers_filtered(node))
        except Exception:
            refs = []

        if not refs:
            log_info(f"{indent}↳ [no non-console referrers]")
            return

        if per_node_limit is not None and len(refs) > per_node_limit:
            log_info(f"{indent}↳ ({len(refs)} referrers, showing first {per_node_limit})")

        limit = len(refs) if per_node_limit is None else min(per_node_limit, len(refs))
        for i in range(limit):
            ref = refs[i]
            label = f"{type(ref).__name__}"
            extra = []
            if isinstance(ref, dict):
                modname = ref.get("__name__", None)
                if modname:
                    extra.append(f"module {modname!r}")
                elif "__file__" in ref and "__name__" in ref:
                    extra.append(f"module {ref['__name__']!r}")
                else:
                    extra.append(f"dict(len={len(ref)})")
            elif isinstance(ref, (list, tuple, set, frozenset)):
                try:
                    extra.append(f"len={len(ref)}")
                except Exception:
                    pass
            elif _threading and isinstance(ref, _threading.Thread):
                extra.append(f"thread name={getattr(ref, 'name', '?')!r}")
            preview = _safe_preview(ref)
            extras = f" [{' | '.join(extra)}]" if extra else ""
            log_info(f"{indent}↳ {label}{extras}: {preview}")
            walk(ref, depth-1, indent + "  ")

    walk(obj, max_depth, "")

def inspect_bv(index, max_depth=DEFAULT_MAX_DEPTH, per_node_limit=DEFAULT_PER_NODE_LIMIT):
    """Inspect a specific BV from list_bvs(); prints filtered backrefs."""
    bvs = live_bvs()
    if not bvs:
        log_info("No live BinaryViews.")
        return None
    if not (0 <= index < len(bvs)):
        log_error(f"Index {index} out of range (0..{len(bvs)-1}).")
        return None
    bv = bvs[index]
    log_info(f"Inspecting BV [{index}]: {_describe_bv(bv)}")
    try:
        log_info(f"sys.getrefcount: {sys.getrefcount(bv)-1}")
    except Exception:
        pass
    print_referrers(bv, max_depth=max_depth, per_node_limit=per_node_limit)
    log_info("Tip: common culprits are module-level globals, caches, timers/threads, or closures capturing the BV.")
    return bv

def kill_ref(varname, module=None):
    """Delete a global reference in the given module (or __main__) and GC."""
    if module is None:
        import __main__ as module
    if hasattr(module, varname):
        try:
            delattr(module, varname)
            log_info(f"Deleted {module.__name__}.{varname}")
        except Exception:
            try:
                del module.__dict__[varname]  # type: ignore[attr-defined]
                log_info(f"Deleted {module.__name__}.__dict__['{varname}']")
            except Exception as e:
                log_error(f"Failed to delete {varname}: {e}")
    else:
        log_info(f"{module.__name__}.{varname} not found.")
    gc.collect()

def show_backrefs_graph(index_or_bv, filename="/tmp/bv_backrefs.png", max_depth=4):
    """Optional visualization if objgraph + Graphviz are available."""
    bv = index_or_bv
    if isinstance(index_or_bv, int):
        bvs = live_bvs()
        if not (0 <= index_or_bv < len(bvs)):
            log_error(f"Index {index_or_bv} out of range.")
            return
        bv = bvs[index_or_bv]
    try:
        import objgraph
    except Exception as e:
        log_error(f"objgraph not available: {e}")
        return

    try:
        objgraph.show_backrefs([bv], max_depth=max_depth, filename=filename)
        log_info(f"Wrote backrefs graph to: {filename}")
        
        # Display the image in an HTML report
        uri = Path(filename).resolve().as_uri()
        html = f"""
        <div style="font-family:system-ui, sans-serif; padding:10px;">
          <div style="margin-bottom:10px;"><b>Backrefs Graph for:</b> {_describe_bv(bv)}</div>
          <div style="margin-bottom:6px; opacity:0.7;">{filename}</div>
          <img src="{uri}" style="max-width:100%; height:auto; image-rendering:auto;" />
        </div>
        """
        show_html_report("Leak Audit: Backrefs Graph", html)
    except Exception as e:
        log_error(f"Failed to create backrefs graph: {e}")

# --------- UI helpers / commands ---------

def _prompt_index(max_index):
    prompt = f"Enter BinaryView index (0..{max_index}):"
    raw = get_text_line_input(prompt, "Leak Audit: Inspect BV")
    if raw is None:
        return None
    try:
        txt = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
        idx = int(txt.strip())
        if not (0 <= idx <= max_index):
            raise ValueError("out of range")
        return idx
    except Exception:
        show_message_box(
            "Leak Audit",
            f"Invalid index. Expected integer in range 0..{max_index}.",
            MessageBoxButtonSet.OKButtonSet,
            MessageBoxIcon.ErrorIcon,
        )
        return None

def _log_header(title):
    bar = "-" * max(8, len(title))
    log_info(f"\n{title}\n{bar}")

def _cmd_list_bvs(view):
    _log_header("Leak Audit: Live BinaryViews")
    list_bvs(show_referrers_count=True)

def _cmd_inspect_bv(view):
    bvs = live_bvs()
    if not bvs:
        show_message_box(
            "Leak Audit",
            "No live BinaryViews to inspect.",
            MessageBoxButtonSet.OKButtonSet,
            MessageBoxIcon.InformationIcon,
        )
        return
    idx = _prompt_index(len(bvs) - 1)
    if idx is None:
        return
    _log_header(f"Leak Audit: Inspecting BV[{idx}]")
    try:
        inspect_bv(idx, max_depth=DEFAULT_MAX_DEPTH, per_node_limit=DEFAULT_PER_NODE_LIMIT)
    except Exception as e:
        log_error(f"inspect_bv failed: {e}")

def _cmd_backrefs_graph(view):
    bvs = live_bvs()
    if not bvs:
        show_message_box(
            "Leak Audit",
            "No live BinaryViews to graph.",
            MessageBoxButtonSet.OKButtonSet,
            MessageBoxIcon.InformationIcon,
        )
        return
    idx = _prompt_index(len(bvs) - 1)
    if idx is None:
        return
    _log_header(f"Leak Audit: Backrefs Graph for BV[{idx}]")
    try:
        outfile = "/tmp/bv_backrefs.png"
        show_backrefs_graph(idx, filename=outfile, max_depth=4)
    except Exception as e:
        log_error(f"Graph generation failed: {e}")

# --------- Register Commands ---------

# Wrap our existing command functions with UIAction-compatible callbacks
def _ua_list_bvs(ctx):
    # ctx is a UIActionContext (unused here)
    _log_header("Leak Audit: Live BinaryViews")
    list_bvs(show_referrers_count=True)

def _ua_inspect_bv(ctx):
    bvs = live_bvs()
    if not bvs:
        show_message_box(
            "Leak Audit",
            "No live BinaryViews to inspect.",
            MessageBoxButtonSet.OKButtonSet,
            MessageBoxIcon.InformationIcon,
        )
        return
    idx = _prompt_index(len(bvs) - 1)
    if idx is None:
        return
    _log_header(f"Leak Audit: Inspecting BV[{idx}]")
    inspect_bv(idx, max_depth=DEFAULT_MAX_DEPTH, per_node_limit=DEFAULT_PER_NODE_LIMIT)

def _ua_backrefs_graph_all(ctx):
    bvs = live_bvs()
    if not bvs:
        show_message_box(
            "Leak Audit",
            "No live BinaryViews to graph.",
            MessageBoxButtonSet.OKButtonSet,
            MessageBoxIcon.InformationIcon,
        )
        return

    # Ensure objgraph is available
    try:
        import objgraph
    except Exception as e:
        show_message_box(
            "Leak Audit",
            f"objgraph not available: {e}",
            MessageBoxButtonSet.OKButtonSet,
            MessageBoxIcon.ErrorIcon,
        )
        return

    _log_header("Leak Audit: Backrefs Graphs for ALL BVs")

    outdir = Path(tempfile.gettempdir()) / "bn_leak_audit"
    try:
        outdir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    results = []
    errors = []

    for idx, bv in enumerate(bvs):
        outfile = outdir / f"bv_backrefs_{idx}.png"
        try:
            # one graph per BV to keep components readable
            objgraph.show_backrefs([bv], max_depth=4, filename=str(outfile))
            results.append((idx, _describe_bv(bv), outfile))
        except Exception as e:
            errors.append((idx, _describe_bv(bv), str(e)))

    # Build a single HTML report with all images
    parts = [
        '<div style="font-family:system-ui, sans-serif; padding:10px;">',
        f'<div style="margin-bottom:10px;">Generated {len(results)} graph(s) to {outdir}</div>'
    ]

    if errors:
        parts.append('<div style="color:#b00; margin-bottom:10px;"><b>Errors:</b><ul>')
        for idx, desc, msg in errors:
            parts.append(f"<li>BV[{idx}] {desc}: {msg}</li>")
        parts.append("</ul></div>")

    for idx, desc, path in results:
        uri = Path(path).resolve().as_uri()
        parts.append(f"""
        <div style="margin:14px 0; padding-bottom:12px; border-bottom:1px solid #ddd;">
          <div style="margin-bottom:6px;"><b>BV[{idx}]</b> — {desc}<br/>
            <span style="opacity:.7;">{path}</span>
          </div>
          <img src="{uri}" style="max-width:100%; height:auto; image-rendering:auto;" />
        </div>
        """)

    parts.append("</div>")
    show_html_report("Leak Audit: Backrefs Graphs (All BVs)", "\n".join(parts))

def _always_enabled(ctx):  # keep menu items always enabled
    return True

# Register actions
UIAction.registerAction("Leak Audit\\List Live BinaryViews")
UIAction.registerAction("Leak Audit\\Inspect BV by Index...")
UIAction.registerAction("Leak Audit\\Backrefs Graphs for All (objgraph)")

# Bind callbacks
UIActionHandler.globalActions().bindAction(
    "Leak Audit\\List Live BinaryViews",
    UIAction(_ua_list_bvs, _always_enabled)
)
UIActionHandler.globalActions().bindAction(
    "Leak Audit\\Inspect BV by Index...",
    UIAction(_ua_inspect_bv, _always_enabled)
)
UIActionHandler.globalActions().bindAction(
    "Leak Audit\\Backrefs Graphs for All (objgraph)",
    UIAction(_ua_backrefs_graph_all, _always_enabled)
)
