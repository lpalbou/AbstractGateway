"""Agency's (b)-lane end-to-end functional proof (coordination task, Laurent's
"make sure everything is functional" directive).

Drives the REAL console route chain a creation modal would call, through the
real gateway FastAPI route table (in-process TestClient), against a fresh temp
data dir — no mocks:

  templates -> validate (dry-run, writes nothing) -> validate a BAD spark
  (refused, name not burned) -> create -> read-back -> verify -> the
  capability-matrix the modal's tools view renders.

Exit 0 only if every leg is functional. This is the runnable proof behind the
(b) row of the report, not a co-signed spec.
"""

from __future__ import annotations

import os
import sys
import tempfile


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="agency-b-e2e-")
    token = "agency-b-e2e-secret"
    os.environ["ABSTRACTGATEWAY_AUTH_TOKEN"] = token
    os.environ["ABSTRACTGATEWAY_DATA_DIR"] = os.path.join(tmp, "runtime")

    from fastapi.testclient import TestClient
    from abstractgateway.app import app

    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{' — ' + detail if detail else ''}")

    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as client:
        # 1. Gallery (pre-create, entity-independent).
        g = client.get("/api/gateway/entities/templates")
        gallery = g.json() if g.status_code == 200 else {}
        tmpls = gallery.get("templates", [])
        default = next((t for t in tmpls if t["id"] == "framework-default"), None)
        check("templates gallery serves framework-default",
              g.status_code == 200 and default is not None,
              f"http={g.status_code} templates={[t['id'] for t in tmpls]}")

        # 2. Dry-run validate a GOOD spark — must be ok AND write nothing.
        spark = dict(default["spark"]) if default else {}
        spark["name"] = "Pollux"
        v = client.post("/api/gateway/entities/pollux/validate",
                        json={"name": "Pollux", "spark": spark})
        vbody = v.json() if v.status_code == 200 else {}
        after = client.get("/api/gateway/entities").json().get("entities", [])
        check("validate(good) ok + writes nothing",
              v.status_code == 200 and vbody.get("ok") is True
              and not any(e.get("slug") == "pollux" for e in after),
              f"ok={vbody.get('ok')} entities_after={[e.get('slug') for e in after]}")

        # 3. Dry-run validate a BAD spark (missing the shared_vulnerability core
        #    value) — must refuse, and STILL write nothing (name not burned).
        bad = {"name": "Pollux", "origin": "no core value", "values": []}
        vb = client.post("/api/gateway/entities/pollux/validate",
                         json={"name": "Pollux", "spark": bad})
        vbb = vb.json() if vb.status_code == 200 else {}
        after_bad = client.get("/api/gateway/entities").json().get("entities", [])
        check("validate(bad) refuses + name not burned",
              vb.status_code == 200 and vbb.get("ok") is False
              and not any(e.get("slug") == "pollux" for e in after_bad),
              f"ok={vbb.get('ok')} errors={(vbb.get('errors') or [])[:1]}")

        # 4. Create for real (the validated good spark).
        c = client.post("/api/gateway/entities", json={"name": "Pollux", "spark": spark})
        check("create -> 201", c.status_code == 201, f"http={c.status_code} {c.text[:120]}")

        # 5. Read back + verify the entity is real.
        rb = client.get("/api/gateway/entities/pollux")
        ver = client.get("/api/gateway/entities/pollux/verify")
        vok = ver.json().get("ok") if ver.status_code == 200 else None
        check("read-back + verify(ok=True)",
              rb.status_code == 200 and ver.status_code == 200 and vok is True,
              f"readback={rb.status_code} verify_ok={vok}")

        # 6. The capability-matrix the modal's tools view renders (pre-create
        #    entity-independent route) — must be the shape uic's component reads.
        m = client.get("/api/gateway/entities/inventory/capability-matrix")
        mb = m.json() if m.status_code == 200 else {}
        phases_ok = isinstance(mb.get("phases"), list) and \
            [p.get("id") for p in mb.get("phases", [])] == ["visit", "work", "personal", "sleep"]
        sections_ok = isinstance(mb.get("sections"), list) and \
            bool(mb.get("sections")) and mb["sections"][0].get("items")
        check("capability-matrix served in renderable shape",
              m.status_code == 200 and phases_ok and sections_ok,
              f"phases={[p.get('id') for p in mb.get('phases', [])]} "
              f"sections={[s.get('id') for s in mb.get('sections', [])]}")

        # 7. Full inventory union (both containments) served non-degraded.
        inv = client.get("/api/gateway/entities/inventory/tools")
        ib = inv.json() if inv.status_code == 200 else {}
        check("inventory union non-degraded",
              inv.status_code == 200 and ib.get("degraded") is False
              and len(ib.get("tools", [])) >= 9,
              f"degraded={ib.get('degraded')} tools={len(ib.get('tools', []))}")

    ok = all(r[1] for r in results)
    print(f"\n(b) END-TO-END: {'ALL FUNCTIONAL' if ok else 'FAILURES PRESENT'} "
          f"({sum(1 for r in results if r[1])}/{len(results)} legs)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
