from __future__ import annotations
import argparse
from .assembler import assemble_dossier
from .store import write_dossier
from .v2 import assemble_institutional_dossier, write_institutional_dossier

def build_parser():
    p=argparse.ArgumentParser(description="Generate a deterministic research dossier from frozen artifacts")
    p.add_argument("--ticker",required=True); p.add_argument("--as-of-date",required=True); p.add_argument("--profile",default="core_v1")
    p.add_argument("--format",choices=("markdown","json","both"),default="both"); p.add_argument("--detail",choices=("concise","standard","full"),default="standard")
    p.add_argument("--include-evidence",action="store_true"); p.add_argument("--explain-scores",action="store_true"); p.add_argument("--force",action="store_true")
    p.add_argument("--artifacts-root",default="artifacts"); p.add_argument("--output-root",default="artifacts/research_output")
    p.add_argument("--output-version",choices=("v1","v2"),default="v1", help="write the backward-compatible v1 dossier or additive research-output-v2 JSON")
    return p
def main(argv=None):
    a=build_parser().parse_args(argv)
    if a.output_version == "v2":
        d = assemble_institutional_dossier(a.ticker, a.as_of_date, a.profile, a.artifacts_root)
        result = write_institutional_dossier(d, a.output_root, force=a.force)
        print(f"Generated {d.ticker} institutional dossier ({d.dossier_id})")
        print(f"Input fingerprint: {d.input_fingerprint}")
        print(f"Quality gate: {d.quality_gate.status}")
        if d.quality_gate.blockers:
            print("Critical blockers:")
            for blocker in d.quality_gate.blockers:
                print(f"- {blocker.get('code', blocker) if isinstance(blocker, dict) else blocker}")
        if d.quality_gate.warnings:
            print("Warnings:")
            for warning in d.quality_gate.warnings:
                print(f"- {warning}")
        print(f"Unchanged: {result['unchanged']}; registry entries: {result['registry_entries']}")
        print(f"json: {result['json']}")
        print(f"markdown: {result['markdown']}")
        return 0
    d=assemble_dossier(a.ticker,a.as_of_date,a.profile,a.artifacts_root)
    result=write_dossier(d,a.output_root,a.format,a.detail,a.include_evidence,a.explain_scores,a.force)
    print(f"Generated {d.ticker} dossier ({d.dossier_id})")
    print(f"Input fingerprint: {d.input_fingerprint}")
    print(f"Unchanged: {result['unchanged']}; registry entries: {result['registry_entries']}")
    for k,v in result["outputs"].items(): print(f"{k}: {v}")
    return 0
if __name__ == "__main__": raise SystemExit(main())
