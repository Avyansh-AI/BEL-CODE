"""Tests for ProjectIntelligence, EngineeringMemory, and SkillsAndCapabilities."""

from pathlib import Path
import pytest

from bel_code.intelligence.indexer import ProjectIntelligence
from bel_code.memory.store import EngineeringMemory
from bel_code.advanced.capabilities import SkillsAndCapabilities


def test_project_intelligence_scanner_symbols_architecture_and_health(tmp_path: Path):
    # Create a miniature sample repository
    (tmp_path / "README.md").write_text("# Sample Service\nFastAPI microservice", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("fastapi>=0.100.0\npytest>=7.0.0\n", encoding="utf-8")
    (tmp_path / "service.py").write_text(
        '"""Core billing service module."""\n\n'
        "class InvoiceProcessor:\n"
        '    """Processes customer invoices."""\n'
        "    def calculate_total(self, subtotal: float, tax_rate: float = 0.1) -> float:\n"
        "        return subtotal * (1.0 + tax_rate)\n\n"
        "def unused_helper_one(x):\n"
        "    return x * 2 + 1\n",
        encoding="utf-8",
    )
    (tmp_path / "api.py").write_text(
        "from fastapi import FastAPI\n"
        "from service import InvoiceProcessor\n\n"
        "app = FastAPI()\n\n"
        "@app.get('/invoices/{invoice_id}')\n"
        "def get_invoice(invoice_id: str):\n"
        "    proc = InvoiceProcessor()\n"
        "    return {'id': invoice_id, 'total': proc.calculate_total(100.0)}\n",
        encoding="utf-8",
    )

    intel = ProjectIntelligence(workspace_root=tmp_path)

    scan = intel.scan_project()
    assert isinstance(scan, dict)
    assert "python" in str(scan).lower()

    symbols = intel.build_symbol_index()
    assert isinstance(symbols, dict)
    assert "InvoiceProcessor" in str(symbols)
    assert "calculate_total" in str(symbols)

    arch = intel.generate_architecture_map()
    assert isinstance(arch, dict)
    mermaid_str = arch.get("mermaid", arch.get("diagram", str(arch)))
    assert "flowchart" in mermaid_str or "graph" in mermaid_str

    ctx = intel.retrieve_context("invoice tax calculation", top_k=3)
    assert isinstance(ctx, list)
    assert len(ctx) >= 1
    assert any("service.py" in str(item) or "calculate_total" in str(item) for item in ctx)

    health = intel.analyze_code_health()
    assert isinstance(health, dict)


def test_engineering_memory_isolation_sanitization_and_session_recovery(tmp_path: Path):
    mem = EngineeringMemory(storage_dir=tmp_path / "mem_store")

    # Add memories to two distinct projects and verify strict project isolation
    rec_a = mem.add_memory(
        project_id="project-alpha",
        category="architecture_decision",
        title="Use PostgreSQL",
        content="Primary database is PostgreSQL 16 with secret key sk-abcdef1234567890abcdef1234567890 and thk_live_99887766554433221100",
    )
    rec_b = mem.add_memory(
        project_id="project-beta",
        category="coding_standard",
        title="Type Annotations",
        content="All functions must use strict type hints",
    )

    alpha_memories = mem.search_memory("project-alpha")
    beta_memories = mem.search_memory("project-beta")

    assert len(alpha_memories) == 1
    assert len(beta_memories) == 1
    assert "PostgreSQL" in str(alpha_memories[0])
    assert "PostgreSQL" not in str(beta_memories)

    # Verify automatic secret sanitization before persistence
    assert "sk-abcdef1234567890abcdef1234567890" not in str(alpha_memories[0])
    assert "thk_live_99887766554433221100" not in str(alpha_memories[0])

    # Update & Export
    mem_id = rec_b["id"] if isinstance(rec_b, dict) else rec_b
    mem.update_memory("project-beta", mem_id, {"content": "Updated type hint rule"})
    exported = mem.export_project_memory("project-beta")
    assert "Updated type hint rule" in str(exported)

    # Session state persistence and resume
    session_state = {
        "stage": "Test",
        "completed_stages": ["Understand", "Plan", "Implement"],
        "verified_steps": ["step-1", "step-2"],
        "pending_subtasks": ["run_regression_suite"],
    }
    mem.save_session_state("project-alpha", "task-77", session_state)
    loaded = mem.load_session_state("project-alpha", "task-77")
    assert loaded is not None
    assert "Implement" in str(loaded)

    # Append-only changelog
    mem.append_changelog("project-alpha", {"task_id": "task-77", "summary": "Implemented invoice tax calculation"})
    changelog = mem.get_changelog("project-alpha")
    assert len(changelog) >= 1


def test_skills_and_advanced_capabilities(tmp_path: Path):
    # Setup legacy Python 2 file + FastAPI route file + vulnerable file for scanning
    legacy_file = tmp_path / "legacy_py2.py"
    legacy_file.write_text(
        'print "Starting legacy job"\n'
        "for i in xrange(5):\n"
        '    print "Item", i\n',
        encoding="utf-8",
    )

    api_file = tmp_path / "routes.py"
    api_file.write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/users')\n"
        "def list_users():\n"
        "    return []\n"
        "@app.post('/orders')\n"
        "def create_order():\n"
        "    return {}\n",
        encoding="utf-8",
    )

    vuln_file = tmp_path / "unsafe.py"
    vuln_file.write_text(
        "import subprocess\n"
        "def run_user_input(cmd, user_id):\n"
        "    query = f\"SELECT * FROM accounts WHERE id = {user_id}\"\n"
        "    subprocess.Popen(cmd, shell=True)\n"
        "    return eval(cmd)\n",
        encoding="utf-8",
    )

    caps = SkillsAndCapabilities(workspace_root=tmp_path)

    # 1. Skills registration & execution
    caps.register_skill("security_audit", "Run full security audit", steps=["scan", "report"])
    skills = caps.list_skills()
    assert any("security_audit" in str(s) for s in skills)
    skill_res = caps.execute_skill("security_audit", context={"target": "unsafe.py"})
    assert isinstance(skill_res, dict)

    # 2. Codebase migration (Python 2 -> Python 3)
    mig = caps.migrate_codebase(migration_type="python2_to_3", target_files=[str(legacy_file)], dry_run=False)
    assert isinstance(mig, dict)
    upgraded_code = legacy_file.read_text(encoding="utf-8")
    assert "range(" in upgraded_code and "xrange(" not in upgraded_code
    assert "print(" in upgraded_code

    # 3. OpenAPI spec generation
    openapi = caps.generate_openapi_spec()
    assert isinstance(openapi, dict)
    assert "/users" in str(openapi) or "openapi" in str(openapi).lower()

    # 4. Database schema analysis
    db_analysis = caps.analyze_database_schema(
        "CREATE TABLE orders (id INT PRIMARY KEY, user_email VARCHAR(255), status VARCHAR(32), created_at TIMESTAMP);"
    )
    assert isinstance(db_analysis, dict)

    # 5. Performance profiling
    perf = caps.profile_performance(target_script=str(api_file))
    assert isinstance(perf, dict)

    # 6. Security vulnerability scan
    sec_scan = caps.run_security_scan()
    assert isinstance(sec_scan, dict)
    findings = sec_scan.get("findings", sec_scan.get("vulnerabilities", []))
    assert len(findings) >= 1

    # 7. CI/CD pipeline generation
    cicd = caps.generate_cicd_pipeline(platform="github_actions")
    assert isinstance(cicd, dict)
    assert "pytest" in str(cicd).lower() or "steps" in str(cicd).lower()

    # 8. Image-to-interface generation
    ui_gen = caps.image_to_interface(
        {"title": "Customer Portal", "components": ["navbar", "metrics_cards", "data_table"]}
    )
    assert isinstance(ui_gen, dict)
    assert "<html" in str(ui_gen).lower() or "<main" in str(ui_gen).lower()

    # 9. Measured self-evaluation
    eval_res = caps.evaluate_agent_history(
        [
            {"task_id": "t1", "status": "verified", "repair_iterations": 0, "tokens": 1200},
            {"task_id": "t2", "status": "verified", "repair_iterations": 2, "tokens": 4500},
        ]
    )
    assert isinstance(eval_res, dict)
