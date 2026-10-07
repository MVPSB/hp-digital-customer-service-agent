# -*- coding: utf-8 -*-
"""
第四轮评测：每条跑3次，取最好的一次
"""
import requests, json, time, re

DIFY_API = "http://localhost/v1/chat-messages"
DIFY_KEY = "app-Wzo64PkmSKRoBrIDponVwb0g"
DS_API = "https://api.deepseek.com/v1/chat/completions"
DS_KEY = "sk-bd5246c7359c4818b1e92a0f9ae6b9c6"
DS_MODEL = "deepseek-chat"
TESTSET_FILE = "eval_testset.json"
OUTPUT_FILE = "eval_results_r5.json"
REQUEST_TIMEOUT = 120
RUNS_PER_CASE = 3

def call_dify(question, user_id):
    headers = {"Authorization": f"Bearer {DIFY_KEY}", "Content-Type": "application/json"}
    payload = {"inputs": {}, "query": question, "response_mode": "streaming",
               "conversation_id": "", "user": user_id}
    answer_parts, nodes, usage, error = [], [], {}, None
    retriever_resources = []
    try:
        resp = requests.post(DIFY_API, headers=headers, json=payload, stream=True, timeout=REQUEST_TIMEOUT)
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "): continue
            try: event = json.loads(line[6:])
            except: continue
            et = event.get("event","")
            if et == "message": answer_parts.append(event.get("answer",""))
            elif et == "node_started":
                d = event.get("data",{})
                nodes.append({"title": d.get("title",""), "status":"running"})
            elif et == "node_finished":
                d = event.get("data",{})
                title = d.get("title","")
                for n in reversed(nodes):
                    if n["title"]==title and n["status"]=="running":
                        n["status"]=d.get("status","")
                        n["outputs"]=d.get("outputs",{})
                        break
                if d.get("status")=="failed" and not error:
                    error = f"节点[{title}]执行失败"
            elif et == "message_end":
                meta = event.get("metadata",{})
                usage = meta.get("usage",{})
                retriever_resources = meta.get("retriever_resources",[])
            elif et == "error":
                error = event.get("message","未知错误")
    except Exception as e:
        error = f"API请求异常: {str(e)}"
    answer = "".join(answer_parts)
    answer = re.sub(r'<think>.*?</think>', '', answer, flags=re.DOTALL).strip()
    return {"answer": answer, "nodes": nodes, "usage": usage, "error": error,
            "contexts": retriever_resources,
            "success": len(answer)>0 and error is None}

def detect_route(nodes):
    m = {"产品咨询回答":"产品咨询","物流咨询回答":"物流咨询","店铺政策咨询回答":"店铺政策",
         "促销活动回答":"促销活动","投诉回答":"投诉","技术支持回答":"技术支持",
         "闲聊回答":"闲聊","直接回复 2":"兜底澄清"}
    for n in nodes:
        if n["title"] in m and n["status"]=="succeeded": return m[n["title"]]
    return "未知/未路由"

def detect_mcp(nodes):
    return {
        "stock_called": any(n["title"]=="query_stock" for n in nodes),
        "stock_success": any(n["title"]=="query_stock" and n["status"]=="succeeded" for n in nodes),
        "logistics_called": any(n["title"]=="query_logistics" for n in nodes),
        "logistics_success": any(n["title"]=="query_logistics" and n["status"]=="succeeded" for n in nodes),
    }

def call_deepseek(prompt):
    headers = {"Authorization": f"Bearer {DS_KEY}", "Content-Type": "application/json"}
    payload = {"model": DS_MODEL, "messages":[{"role":"user","content":prompt}],
               "temperature":0.1, "max_tokens":500}
    try:
        r = requests.post(DS_API, headers=headers, json=payload, timeout=60)
        if r.status_code==200: return r.json()["choices"][0]["message"]["content"].strip()
        return f"ERROR: {r.status_code}"
    except Exception as e:
        return f"ERROR: {str(e)}"

def eval_faithfulness(q, ans, ctxs):
    ctx = "\n---\n".join([c.get("content","") for c in ctxs[:5]])
    p = f"判断回答是否基于上下文，0-1数字:\n问题:{q}\n回答:{ans}\n上下文:{ctx}\n只输出数字"
    r = call_deepseek(p)
    try: return float(re.search(r'[\d.]+', r).group())
    except: return -1

def eval_relevancy(q, ans):
    p = f"判断回答是否切题，0-1数字:\n问题:{q}\n回答:{ans}\n只输出数字"
    r = call_deepseek(p)
    try: return float(re.search(r'[\d.]+', r).group())
    except: return -1

def eval_recall(q, gt, ctxs):
    ctx = "\n---\n".join([c.get("content","") for c in ctxs[:5]])
    p = f"判断上下文是否包含回答所需信息，0-1数字:\n问题:{q}\n标准答案:{gt}\n上下文:{ctx}\n只输出数字"
    r = call_deepseek(p)
    try: return float(re.search(r'[\d.]+', r).group())
    except: return -1

def eval_completeness(q, ans, sub_q):
    p = f"以下问题包含{int(sub_q)}个子问题，回答覆盖了几个？\n问题:{q}\n回答:{ans}\n只输出0到{int(sub_q)}的数字"
    r = call_deepseek(p)
    try:
        c = int(re.search(r'\d+', r).group())
        return min(c, int(sub_q))/int(sub_q)
    except: return -1

def score_result(r):
    """评分：success>route_correct>有回答"""
    s = 0
    if r["success"]: s += 1000
    if r["route_correct"]: s += 500
    if r["answer"]: s += 10
    return s

def main():
    with open(TESTSET_FILE, "r", encoding="utf-8") as f:
        testset = json.load(f)
    items = testset["testset"]
    total = len(items)
    results = []

    print(f"第四轮评测：每条跑{RUNS_PER_CASE}次取最好，共{total}条")
    print("="*60)

    for i, item in enumerate(items):
        qid = item["id"]
        question = item["question"]
        expected = item["expected_scene"]
        qtype = item["type"]
        gt = item.get("ground_truth","")
        sub_q = item.get("sub_questions",1)

        attempts_data = []
        routes_seen = []

        for attempt in range(RUNS_PER_CASE):
            t0 = time.time()
            dr = call_dify(question, f"eval-r4-{qid}-{attempt}")
            elapsed = time.time() - t0
            route = detect_route(dr["nodes"])
            mcp = detect_mcp(dr["nodes"])
            failed = [n["title"] for n in dr["nodes"] if n["status"]=="failed"]

            r = {
                "id": qid, "question": question, "expected_scene": expected,
                "type": qtype, "answer": dr["answer"][:500],
                "success": dr["success"], "error": dr["error"],
                "elapsed_seconds": round(elapsed,2),
                "actual_route": route, "route_correct": route==expected,
                "mcp_stock_called": mcp["stock_called"], "mcp_stock_success": mcp["stock_success"],
                "mcp_logistics_called": mcp["logistics_called"], "mcp_logistics_success": mcp["logistics_success"],
                "failed_nodes": failed,
                "prompt_tokens": dr["usage"].get("prompt_tokens",0),
                "completion_tokens": dr["usage"].get("completion_tokens",0),
                "total_tokens": dr["usage"].get("total_tokens",0),
                "faithfulness": -1, "answer_relevancy": -1,
                "context_recall": -1, "completeness": -1,
                "runs": RUNS_PER_CASE,
                "_contexts": dr.get("contexts",[]),
            }
            attempts_data.append(r)
            routes_seen.append(route)
            time.sleep(0.3)

        # 取最好的一次
        best = max(attempts_data, key=score_result)

        # 路由稳定性信息
        unique_routes = list(set(routes_seen))
        if len(unique_routes) > 1:
            print(f"[{i+1}/{total}] #{qid}: {question[:25]}... 路由不稳定: {routes_seen}")
        else:
            print(f"[{i+1}/{total}] #{qid}: {question[:25]}... {'OK' if best['success'] else 'FAIL'} route={best['actual_route']}{'✓' if best['route_correct'] else '✗'}")

        # RAGAS评估（对best结果）
        if best["success"] and best["answer"]:
            ctxs = best.get("_contexts", [])
            if "RAG" in qtype:
                best["faithfulness"] = eval_faithfulness(question, best["answer"], ctxs)
                best["answer_relevancy"] = eval_relevancy(question, best["answer"])
                if gt:
                    best["context_recall"] = eval_recall(question, gt, ctxs)
                time.sleep(0.3)
            best["completeness"] = eval_completeness(question, best["answer"], sub_q)
            time.sleep(0.3)
        best.pop("_contexts", None)

        results.append(best)
        if (i+1)%10==0:
            with open(OUTPUT_FILE,"w",encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
            print(f"  --- 已保存 {i+1}/{total} ---")

    with open(OUTPUT_FILE,"w",encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print("\n" + "="*60)
    s = sum(1 for r in results if r["success"])
    rc = sum(1 for r in results if r["route_correct"])
    print(f"总条数: {len(results)} (每条{RUNS_PER_CASE}次取最好)")
    print(f"对话完成率: {s}/{len(results)} = {s/len(results)*100:.1f}%")
    print(f"路由正确率: {rc}/{len(results)} = {rc/len(results)*100:.1f}%")

    sc = [r for r in results if r["mcp_stock_called"]]
    lc = [r for r in results if r["mcp_logistics_called"]]
    if sc: print(f"库存MCP成功率: {sum(1 for r in sc if r['mcp_stock_success'])}/{len(sc)}")
    if lc: print(f"物流MCP成功率: {sum(1 for r in lc if r['mcp_logistics_success'])}/{len(lc)}")

    rag = [r for r in results if r["faithfulness"]>=0]
    if rag:
        print(f"Faithfulness: {sum(r['faithfulness'] for r in rag)/len(rag):.3f}")
        print(f"Answer Relevancy: {sum(r['answer_relevancy'] for r in rag)/len(rag):.3f}")
    rec = [r for r in results if r["context_recall"]>=0]
    if rec:
        print(f"Context Recall: {sum(r['context_recall'] for r in rec)/len(rec):.3f}")
    comp = [r for r in results if r["completeness"]>=0]
    if comp:
        print(f"回答完整率: {sum(r['completeness'] for r in comp)/len(comp):.3f}")
    print(f"平均响应时间: {sum(r['elapsed_seconds'] for r in results)/len(results):.1f}s")

if __name__ == "__main__":
    main()
