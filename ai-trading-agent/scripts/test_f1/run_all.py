"""F1 test runner — to'rttasini ketma-ket chaqiradi va summary chiqaradi"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
SCRIPTS = [
    "test_db_layer.py",
    "test_admin_cli.py",
    "test_self_learner_gate.py",
    "test_auto_apply_mode.py",
    "test_config_validation.py",
    "test_env_example.py",
    "test_health.py",
]

results = []
for s in SCRIPTS:
    print(f"\n>>> Running {s}")
    p = subprocess.run(
        [sys.executable, str(HERE / s)],
        capture_output=False,
    )
    results.append((s, p.returncode))

print("\n" + "=" * 50)
print("F1 TEST SUMMARY")
print("=" * 50)
for s, rc in results:
    status = "PASS" if rc == 0 else "FAIL"
    print(f"  {status:4} - {s}")

total_failed = sum(1 for _, rc in results if rc != 0)
print(f"\nTotal: {len(results) - total_failed}/{len(results)} scripts passed")
sys.exit(total_failed)
