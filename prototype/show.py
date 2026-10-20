import json, sys
tag, ids = sys.argv[1], set(sys.argv[2].split(","))
limit = int(sys.argv[3]) if len(sys.argv) > 3 else 1
shown = {}
for line in open("events_qwen_suite.jsonl"):
    rec = json.loads(line)
    if rec["tag"] != tag: continue
    res = rec["result"]
    if res["id"] not in ids or shown.get(res["id"], 0) >= limit: continue
    shown[res["id"]] = shown.get(res["id"], 0) + 1
    print("=" * 70)
    print(res["id"], "PASS" if res["passed"] else "FAIL", res["failures"])
    for t in res["turns"]:
        print(" user :", t["user"])
        for c in t["calls"]:
            print("  call:", c["action"], c["args"], "->", "ok" if c["result"].get("ok") else c["result"])
        print(" final:", t["final_kind"], "|", t["final_message"][:300])
