"""Read-only source preflight; never builds, installs, signs or starts a Worker.

An installer/launcher EXE with a private CPython runtime can preserve today's
script process boundaries. Directly freezing agent.py is a separate migration.
"""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path


REQUIRED_SOURCE_FILES = (
    "agent.py", "identity.py", "identity_setup.py", "safety.py", "storage_paths.py",
    "job_state.py", "installer_process.py", "process_control.py", "worker_legal.py",
    "local_sandbox.py", "legal_policy.json", "legal_evidence.json", "legal_sources/INDEX.json", "run_worker.ps1", "install_linux.sh",
    "installers/component_installer.py", "installers/model_installer.py",
    "installers/model_catalog.py", "installers/python_runtime.py",
    "runners/comfyui.py", "runners/hunyuan3d.py", "runners/freecad.py",
)


def inspect_sources(root):
    root = Path(root).resolve()
    missing = [name for name in REQUIRED_SOURCE_FILES if not (root / name).is_file()]
    findings = []
    if missing:
        findings.append({"id": "missing-source-files", "severity": "blocker", "files": missing,
                         "remedy": "Restore the complete reviewed source package before packaging."})
    # No config contents, credentials, caches, binaries or models are read.
    personalized = [name for name in ("config.json", "config/identity", "runtime/worker-journal.sqlite3")
                    if (root / name).exists()]
    if personalized:
        findings.append({"id": "personalized-worker", "severity": "blocker", "files": personalized,
                         "remedy": "Package clean reviewed sources; never redistribute an existing Worker identity."})
    agent_path = root / "agent.py"
    version, interpreter_calls = None, []
    if agent_path.is_file():
        source = agent_path.read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "AGENT_VERSION" for target in node.targets):
                version = ast.literal_eval(node.value)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "sys" and node.attr == "executable":
                interpreter_calls.append(node.lineno)
        if interpreter_calls:
            findings.append({"id": "python-child-processes", "severity": "direct-freeze-blocker",
                             "file": "agent.py", "lines": sorted(interpreter_calls),
                             "remedy": "Keep an explicit private CPython interpreter for script installers/runners; a frozen sys.executable is the app, not Python."})
    findings.extend((
        {"id": "source-updater-contract", "severity": "direct-freeze-blocker",
         "remedy": "The updater validates/replaces Python files and parses AGENT_VERSION; design signed versioned binaries plus atomic rollback before replacing that contract."},
        {"id": "supervisor-contract", "severity": "direct-freeze-blocker",
         "remedy": "Windows supervisor and Linux systemd execute agent.py with Python. Preserve exit 64, locks, job journal and identity, then test restart/update separately."},
        {"id": "external-engine-assets", "severity": "release-gate",
         "remedy": "Keep engines, models, venvs, caches, data and licenses outside the executable; retain the existing root/manifest and user-installed resources."},
        {"id": "server-injected-adapters", "severity": "release-gate",
         "remedy": "The server package builder adds hunyuan_worker.py, freecad_converter.py, freecad_builder.py and orca_core_bridge.ps1 from project sources. Validate the final manifest, not worker_agent alone."},
        {"id": "publisher-signature", "severity": "release-gate",
         "remedy": "Use the real publisher's code-signing certificate, timestamp and independent signature verification; no certificate is created or claimed by this preflight."},
        {"id": "native-platform-validation", "severity": "release-gate",
         "remedy": "Run lifecycle and engine smoke tests on each supported native OS/architecture. macOS/ARM are not implemented by the current Worker."},
    ))
    return {"schema_version": 1, "read_only": True, "agent_version": version,
            "source_layout_complete": not missing, "clean_source_identity": not personalized,
            "ready_for_direct_freeze": False, "release_ready": False,
            "recommended_first_package": "signed-installer-and-launcher-with-private-cpython",
            "findings": findings}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parent)
    options = parser.parse_args(argv)
    print(json.dumps(inspect_sources(options.source_root), ensure_ascii=True, indent=2))
    return 2  # Explicitly not a release approval, even if source files are complete.


if __name__ == "__main__":
    raise SystemExit(main())
