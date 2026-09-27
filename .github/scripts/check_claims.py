#!/usr/bin/env python3
"""svc-hello's claims check: render claims.yaml the way the platform will.

Why it exists
-------------
From M2b, the Kubernetes API server no longer checks a database claim: the
claim is a values file, and the platform's charts/claims renders it at Argo CD
(ADR-0017). The first place a bad claim can be caught is here, on the pull
request, by running the same `helm template` against the same charts and the
same platform facts. ADR-0017 §6 names this check the first gate.

It needs no token: platform-config and systems are public.

What it does
------------
  1. claims.yaml sits at the repo root, spelled exactly that way. Argo CD
     skips a missing values file in silence, so `claims.yml`, or a claims.yaml
     inside k8s/, would quietly give this service no database (ADR-0017 §4).
  2. Renders charts/system with this service's tenant file, the retired list
     and the environment file, and takes the valuesObject it writes into the
     svc-hello-claims Application.
  3. Checks that every claim in .github/checks/must-fail.yaml is REFUSED, and
     for the reason it names. If the charts stopped refusing, this check would
     otherwise go on passing.
  4. Renders charts/claims with claims.yaml, then the valuesObject, which wins,
     as it does under Argo CD. It must render.

Run it
------
  HELM=helm-3.19.x python3 .github/scripts/check_claims.py \\
      --platform-config ../platform-config --systems ../systems
"""

import argparse
import os
import pathlib
import subprocess
import sys
import tempfile

import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
SYSTEM = "svc-hello"
HELM = os.environ.get("HELM", "helm")
failures = []


def report(ok, message):
    print(("ok   " if ok else "FAIL ") + message)
    if not ok:
        failures.append(message)


def helm_template(chart_dir, release, value_files):
    command = [HELM, "template", release, str(chart_dir), "--namespace", SYSTEM]
    for path in value_files:
        command += ["-f", str(path)]
    result = subprocess.run(command, capture_output=True, text=True)
    return result.returncode == 0, result.stdout if result.returncode == 0 else result.stderr


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--platform-config", type=pathlib.Path, required=True)
    parser.add_argument("--systems", type=pathlib.Path, required=True)
    args = parser.parse_args()
    charts = args.platform_config / "charts"

    print("== claims.yaml is where the platform reads it")
    report((REPO / "claims.yaml").is_file(), "claims.yaml exists at the repo root")
    for wrong in ("claims.yml", "k8s/claims.yaml", "k8s/claims.yml"):
        report(not (REPO / wrong).exists(), f"no {wrong} (the platform would never read it)")

    print("\n== the platform's facts for this System")
    ok, output = helm_template(charts / "system", f"{SYSTEM}-system", [
        args.systems / "tenants" / f"{SYSTEM}.yaml",
        args.systems / "retired.yaml",
        args.platform_config / "environments" / "reference.yaml",
    ])
    report(ok, f"charts/system renders for {SYSTEM}")
    if not ok:
        print(output)
        sys.exit(1)
    claims_app = [d for d in yaml.safe_load_all(output) if d and d["kind"] == "Application" and d["metadata"]["name"] == f"{SYSTEM}-claims"]
    values_object = claims_app[0]["spec"]["sources"][0]["helm"]["valuesObject"]

    with tempfile.TemporaryDirectory() as folder:
        vo_file = pathlib.Path(folder) / "valuesobject.yaml"
        vo_file.write_text(yaml.safe_dump(values_object))

        print("\n== the check refuses what it must (ADR-0019 §4)")
        for case in yaml.safe_load((REPO / ".github" / "checks" / "must-fail.yaml").read_text()):
            bad = pathlib.Path(folder) / "bad.yaml"
            bad.write_text(yaml.safe_dump(case["claims"]))
            ok, output = helm_template(charts / "claims", f"{SYSTEM}-claims", [bad, vo_file])
            report(not ok and case["expect"] in output, f"refused: {case['name']}")
            if ok or case["expect"] not in output:
                print(output)

        print("\n== this service's claims.yaml")
        ok, output = helm_template(charts / "claims", f"{SYSTEM}-claims", [REPO / "claims.yaml", vo_file])
        report(ok, "claims.yaml renders against the platform's charts")
        if ok:
            for document in yaml.safe_load_all(output):
                if document:
                    print(f"     {document['kind']} {document['metadata']['name']}")
        else:
            print(output)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed.")
        sys.exit(1)
    print("every check passed.")


if __name__ == "__main__":
    main()
