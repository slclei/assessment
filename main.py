import argparse
import json
from scenarios.greenfield import run_greenfield_scenario
from scenarios.brownfield import run_brownfield_scenario
from scenarios.ambiguous import run_ambiguous_scenario
from src.agent_provider import configured_agent_provider

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run a local goal-driven SDLC workflow.")
    parser.add_argument("scenario", choices=["greenfield", "brownfield", "ambiguous"], nargs="?", default="greenfield")
    parser.add_argument("--prompt", help="Requirement prompt for the ambiguous scenario.")
    parser.add_argument("--legacy-module", help="Python module to audit in the brownfield scenario.")
    arguments = parser.parse_args()
    scenario = arguments.scenario
    
    if scenario == "greenfield":
        engine, success = run_greenfield_scenario()
    elif scenario == "brownfield":
        engine, success = run_brownfield_scenario(arguments.legacy_module)
    elif scenario == "ambiguous":
        engine, success = run_ambiguous_scenario(arguments.prompt, configured_agent_provider())

    metrics = engine.get_metrics()
    print("\n=== FINAL AUDIT & RELIABILITY METRICS ==")
    print(json.dumps(metrics, indent=4))
    
    # Exit nonzero if workflow failed or completed with errors
    if not success or not metrics.get("success", False):
        print(f"\n[ERROR] Scenario '{scenario}' failed or encountered governance halts.")
        raise SystemExit(1)
    else:
        print(f"\n[SUCCESS] Scenario '{scenario}' executed successfully.")
        raise SystemExit(0)