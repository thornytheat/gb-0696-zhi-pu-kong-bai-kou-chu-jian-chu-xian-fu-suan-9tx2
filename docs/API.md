# 接口

`POST /api/run`：{"text":"CSV内容"}。同规则版本与输入摘要只产生一条批次。

`POST /api/amend`：{"id":"批次","text":"补交CSV","revision":可选整数}。把补交行并入同一批次，不新增批次。逐行返回更正状态：已生效（进入现行待复核结果）、停在原结论（样品已复核，原结论保留，仅存证）、内容一致、仍隔离。同一 digest 的补交重试直接返回上次记录（replayed=true），不新增版本（幂等优先于版本校验）；首次合并时版本不符返回409；合并失败返回422且旧版结果不变。

`GET/POST /api/1` 至 `/api/7`：独立样例或自定义文件计算。各模块返回 report、quarantine、balance。

`POST /api/review`：id、整数revision、decision取通过或退回。旧版本409；阻断项不能通过。通过即把该版本全部样品标记为已复核，之后补交不再改写这些样品的结论。

`GET /api/runs/{id}/versions`：版本快照清单（被替换版本的原结果保留在库中）。

`GET /api/export/{id}`：唯一证据CSV。`GET /health`：存活检查。证据含 result、quarantine，以及 amendment（逐行更正状态与被替换行依据）、amend-manifest、version（历史版本）记录。

错误：结构/数值422，版本冲突409，不存在404，文件过大413。逐行异常进入quarantine。
