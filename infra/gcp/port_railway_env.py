#!/usr/bin/env python3
"""
Port a Railway service's environment to Cloud Run inputs.

  infra/gcp/port_railway_env.py --railway-service web --railway-env production --env staging \
      [--project pivota-staging] [--apply]

Reads `railway variables --json` (run from a directory linked to the Railway project), then:
  * DROPS  RAILWAY_* (the platform shim derives these), PORT, and DATABASE_URL / REDIS_URL
           (those come from Secret Manager, created by bootstrap_env.sh)
  * SPLITS the rest into
      - secrets: name matches SECRET_NAME_RE or value looks like a credentialed URL
                 -> Secret Manager `env-<NAME>` (created/updated only with --apply)
      - plain:   everything else -> infra/gcp/env.<env>.yaml (git-ignored), consumed by
                 deploy_backend.sh via --env-vars-file
  * APPLIES overrides from infra/gcp/env.<env>.overrides.yaml (git-ignored) last — this is
    where staging gets test-mode keys, staging hostnames, and disabled side-effect flags.
  * PRINTS a review table: every var NAME, its class, and whether it looks LIVE-MODE
    (sk_live_, whsec_, live hostnames...). No secret VALUES are ever printed.

Nothing is written to GCP without --apply. The YAML/overrides files are git-ignored.
"""
import argparse, json, os, re, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SECRET_NAME_RE = re.compile(r"(SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE|CREDENTIAL|API_KEY|APIKEY|_KEY$|_KEY_|DSN|SIGNING|WEBHOOK_SIGN|CLIENT_SECRET|AUTH_STRING|JWT)", re.I)
LIVE_VALUE_RE = re.compile(r"(sk_live_|rk_live_|pk_live_|whsec_|live_|_prod|production)", re.I)
CRED_URL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://[^/@\s]+:[^/@\s]+@", re.I)
DROP_EXACT = {"PORT", "DATABASE_URL", "REDIS_URL", "DATABASE_PUBLIC_URL", "REDIS_PUBLIC_URL"}
DROP_PREFIX = ("RAILWAY_", "NIXPACKS_", "RAILPACK_")

def railway_vars(service, env):
    out = subprocess.run(["railway", "variables", "-s", service, "-e", env, "--json"], capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"railway variables failed: {out.stderr.strip()}")
    return json.loads(out.stdout)

def load_yaml_simple(p):
    """tiny 'KEY: value' / 'KEY: "value"' reader so we don't need PyYAML for overrides."""
    d = {}
    if not p.exists(): return d
    for line in p.read_text().splitlines():
        line = line.rstrip()
        if not line or line.lstrip().startswith("#") or ":" not in line: continue
        k, v = line.split(":", 1); v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'": v = v[1:-1]
        d[k.strip()] = v
    return d

def yaml_quote(v):
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--railway-service", required=True)
    ap.add_argument("--railway-env", default="production")
    ap.add_argument("--env", required=True, choices=["staging", "prod"])
    ap.add_argument("--project")
    ap.add_argument("--apply", action="store_true", help="create/update Secret Manager secrets")
    ap.add_argument("--gcloud", default=os.environ.get("GCLOUD", "gcloud"))
    a = ap.parse_args()
    project = a.project or f"pivota-{a.env}"

    raw = railway_vars(a.railway_service, a.railway_env)
    overrides = load_yaml_simple(HERE / f"env.{a.env}.overrides.yaml")
    plain, secrets, dropped, rows = {}, {}, [], []
    for k in sorted(raw):
        v = raw[k]
        if not isinstance(v, str): v = json.dumps(v)
        if k in DROP_EXACT or k.startswith(DROP_PREFIX):
            dropped.append(k); continue
        is_secret = bool(SECRET_NAME_RE.search(k)) or bool(CRED_URL_RE.match(v))
        src = "railway"
        if k in overrides:
            v = overrides.pop(k); src = "override"
        live = bool(LIVE_VALUE_RE.search(v)) or bool(LIVE_VALUE_RE.search(k))
        (secrets if is_secret else plain)[k] = v
        rows.append((k, "secret" if is_secret else "plain", src, "LIVE?" if live else ""))
    for k, v in overrides.items():  # overrides may add vars that Railway doesn't have
        is_secret = bool(SECRET_NAME_RE.search(k)) or bool(CRED_URL_RE.match(v))
        (secrets if is_secret else plain)[k] = v
        rows.append((k, "secret" if is_secret else "plain", "override(new)", ""))

    # plain env -> yaml
    out_yaml = HERE / f"env.{a.env}.yaml"
    out_yaml.write_text("".join(f"{k}: {yaml_quote(v)}\n" for k, v in sorted(plain.items())))
    # secrets list -> deploy mapping (names only; safe to commit? no — keep git-ignored, names reveal integrations)
    (HERE / f"secrets.{a.env}.list").write_text("".join(f"{k}=env-{k}:latest\n" for k in sorted(secrets)))

    print(f"\n{'VAR':55} {'class':7} {'source':14} live-mode?")
    for r in rows: print(f"{r[0]:55} {r[1]:7} {r[2]:14} {r[3]}")
    print(f"\nplain={len(plain)} secrets={len(secrets)} dropped={len(dropped)} ({', '.join(dropped[:6])}{'...' if len(dropped)>6 else ''})")
    print(f"wrote {out_yaml.name}, secrets.{a.env}.list (both git-ignored)")
    flagged = [r[0] for r in rows if r[3]]
    if a.env == "staging" and flagged:
        print(f"\n!! {len(flagged)} vars look LIVE-MODE and have no override for staging — review before deploying:\n   " + "\n   ".join(flagged))

    if a.apply:
        for k, v in sorted(secrets.items()):
            name = f"env-{k}"
            exists = subprocess.run([a.gcloud, "secrets", "describe", name, "--project", project], capture_output=True).returncode == 0
            if not exists:
                subprocess.run([a.gcloud, "secrets", "create", name, "--replication-policy=automatic", "--project", project], check=True, capture_output=True)
            # only add a version if the value changed
            cur = subprocess.run([a.gcloud, "secrets", "versions", "access", "latest", "--secret", name, "--project", project], capture_output=True, text=True)
            if cur.returncode == 0 and cur.stdout == v:
                continue
            with tempfile.NamedTemporaryFile("w", delete=False) as f:
                f.write(v); tmp = f.name
            try:
                subprocess.run([a.gcloud, "secrets", "versions", "add", name, "--data-file", tmp, "--project", project], check=True, capture_output=True)
            finally:
                os.unlink(tmp)
            print(f"  secret {name}: {'created' if not exists else 'new version'}")
        print(f"applied {len(secrets)} secrets to {project}")
    else:
        print("\n(dry run — pass --apply to write secrets to Secret Manager)")

if __name__ == "__main__":
    main()
