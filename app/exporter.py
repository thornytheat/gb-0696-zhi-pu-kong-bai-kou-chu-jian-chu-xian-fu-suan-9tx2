import csv,io,json

def evidence_csv(run, versions=None):
    out=io.StringIO();writer=csv.writer(out)
    writer.writerow(["run_id","sha256","rule_version","kind","line","reason","evidence"])
    result=run["result"]
    writer.writerow([run["id"],run["digest"],run["rule_version"],"manifest","","",json.dumps({"balance":result["balance"],"decision":run["decision"],"revision":run["revision"],"amendments":len(result.get("amendments",[]))},ensure_ascii=False)])
    for row in result.get("final",[]):
        writer.writerow([run["id"],run["digest"],run["rule_version"],"result",row.get("line",""),
                         row.get("status",""),json.dumps(row,ensure_ascii=False)])
    for row in result.get("quarantine",[]):
        writer.writerow([run["id"],run["digest"],run["rule_version"],"quarantine",row.get("line",""),
                         row["reason"],json.dumps(row,ensure_ascii=False)])
    # 补交更正：逐行标注已生效/停在原结论，并附被替换行的原始依据
    for record in result.get("amendments",[]):
        summary={k:v for k,v in record.items() if k!="rows"}
        writer.writerow([run["id"],run["digest"],record.get("rule_version",run["rule_version"]),
                         "amend-manifest","","",json.dumps(summary,ensure_ascii=False)])
        for entry in record.get("rows",[]):
            writer.writerow([run["id"],run["digest"],record.get("rule_version",run["rule_version"]),
                             "amendment",entry.get("line",""),entry.get("status",""),
                             json.dumps(entry,ensure_ascii=False)])
    # 版本快照：被替换版本的原结论不因合并而湮灭
    for v in versions or []:
        writer.writerow([run["id"],v["digest"],run["rule_version"],"version",v["revision"],"",json.dumps(v,ensure_ascii=False)])
    return "﻿"+out.getvalue()
