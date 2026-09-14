from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

from app import db
from app.settings import (
    ASSISTANT_MIN_RELEVANCE_SCORE,
    CHAT_MODEL_NAME,
    DASHSCOPE_BASE_HTTP_API_URL,
    DASHSCOPE_TIMEOUT,
    EMBEDDING_MODEL_NAME,
    KEYWORD_WEIGHT,
    TOP_K,
    USE_DASHSCOPE_EMBEDDING,
    USE_LLM,
    VECTOR_WEIGHT,
)


logger = logging.getLogger(__name__)

LAW_REFERENCE_PATTERNS = [
    re.compile(r"《(?P<law>[^》\n]{2,80})》第(?P<article>[一二三四五六七八九十百千万零〇0-9]+)条"),
    re.compile(r"(?P<law>[\u4e00-\u9fff]{2,40}(?:法典|法律|法))第(?P<article>[一二三四五六七八九十百千万零〇0-9]+)条"),
]
CASE_REFERENCE_PATTERN = re.compile(r"[（(]\d{4}[）)][^（）()\s]{1,30}号")
ABSOLUTE_TERMS = ["一定胜诉", "必然胜诉", "法院必定", "一定支持", "绝对可以", "保证胜诉"]
LEGAL_DISCLAIMER = "本内容仅供教学、信息检索和初步分析使用，不构成法律意见；涉及具体维权、诉讼或重大权益，请咨询执业律师。"


def _chinese_number_to_int(value: str) -> int | None:
    value = value.strip()
    if value.isdigit():
        return int(value)
    mapping = {"零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    units = {"十": 10, "百": 100, "千": 1000, "万": 10000}
    total = 0
    current = 0
    for char in value:
        if char in mapping:
            current = mapping[char]
        elif char in units:
            total += (current or 1) * units[char]
            current = 0
        else:
            return None
    return total + current


def _normalize_law_name(value: str) -> str:
    return re.sub(r"[\s《》()（）]", "", value).removeprefix("中华人民共和国")


def _law_references(text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pattern in LAW_REFERENCE_PATTERNS:
        for match in pattern.finditer(text):
            article = _chinese_number_to_int(match.group("article"))
            if article is None:
                continue
            law_name = match.group("law").strip()
            key = f"{_normalize_law_name(law_name)}#{article}"
            if key in seen:
                continue
            seen.add(key)
            items.append(
                {
                    "type": "law",
                    "reference": match.group(0),
                    "law_name": law_name,
                    "article_no": article,
                    "key": key,
                    "span": match.span(),
                }
            )
    return items


def _case_references(text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in CASE_REFERENCE_PATTERN.finditer(text):
        reference = match.group(0)
        key = reference.replace("(", "（").replace(")", "）")
        if key in seen:
            continue
        seen.add(key)
        items.append({"type": "case", "reference": reference, "key": key, "span": match.span()})
    return items


def _registered_references() -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = db.rows_to_dicts(
            conn.execute(
                """
                SELECT d.id AS document_id, d.title, d.document_type, d.validity_status,
                       d.validity_date, d.validity_note, c.content
                FROM legal_documents d
                JOIN document_chunks c ON c.document_id = d.id
                WHERE d.status = 'published'
                """
            ).fetchall()
        )
    registry: list[dict[str, Any]] = []
    for row in rows:
        source = f"{row['title']}\n{row['content']}"
        if row["document_type"] == "statute":
            for item in _law_references(source):
                registry.append(item | row)
        elif row["document_type"] == "case":
            for item in _case_references(source):
                registry.append(item | row)
    return registry


def audit_legal_references(text: str) -> dict[str, Any]:
    requested = _law_references(text) + _case_references(text)
    registry = _registered_references()
    items: list[dict[str, Any]] = []
    for item in requested:
        match = next(
            (
                candidate
                for candidate in registry
                if candidate["type"] == item["type"]
                and (
                    candidate["key"] == item["key"]
                    if item["type"] == "case"
                    else candidate["article_no"] == item["article_no"]
                    and _normalize_law_name(item["law_name"]) in _normalize_law_name(candidate["law_name"])
                )
            ),
            None,
        )
        if match:
            items.append(
                {
                    "reference": item["reference"],
                    "type": item["type"],
                    "verified": True,
                    "document_id": match["document_id"],
                    "title": match["title"],
                    "validity_status": match.get("validity_status") or "active",
                    "validity_date": match.get("validity_date") or "",
                    "validity_note": match.get("validity_note") or "",
                }
            )
        else:
            items.append(
                {
                    "reference": item["reference"],
                    "type": item["type"],
                    "verified": False,
                    "reason": "知识库中未找到对应的法条或案号",
                }
            )
    verified = sum(1 for item in items if item["verified"])
    total = len(items)
    return {
        "items": items,
        "total": total,
        "verified": verified,
        "unverified": total - verified,
        "citation_accuracy": round(verified / total * 100, 2) if total else None,
        "hallucination_rate": round((total - verified) / total * 100, 2) if total else 0.0,
        "passed": not any(not item["verified"] for item in items),
    }


def redact_unverified_references(text: str, reference_audit: dict[str, Any]) -> str:
    for item in reference_audit["items"]:
        if not item["verified"]:
            text = text.replace(item["reference"], "【未通过知识库核验的引用已移除】")
    return text


def validity_notices(evidence: list[dict[str, Any]]) -> list[str]:
    notices: list[str] = []
    seen: set[int] = set()
    for item in evidence:
        document_id = int(item.get("document_id") or 0)
        if document_id in seen:
            continue
        seen.add(document_id)
        status = item.get("validity_status") or "active"
        if status == "active":
            continue
        date = item.get("validity_date") or ""
        note = item.get("validity_note") or ""
        label = "已废止" if status == "repealed" else "已修订"
        suffix = f"该条已于 {date} {label}。" if date else f"该条状态为{label}。"
        notices.append(note or f"《{item.get('title', '知识库法规')}》：{suffix}")
    return notices


def case_retrieval_metrics(case_cause: str, case_evidence: list[dict[str, Any]]) -> dict[str, Any]:
    top_cases: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in case_evidence:
        doc_id = int(item["document_id"])
        if doc_id in seen:
            continue
        seen.add(doc_id)
        top_cases.append(
            {
                "document_id": doc_id,
                "title": item["title"],
                "case_cause": item.get("case_cause") or "",
                "relevance_score": item.get("relevance_score", 0),
            }
        )
        if len(top_cases) == 5:
            break
    matches = [item for item in top_cases if item["case_cause"] == case_cause]
    return {
        "expected_case_cause": case_cause,
        "top5_count": len(top_cases),
        "same_cause_hits": len(matches),
        "same_cause_ratio_at_5": round(len(matches) / len(top_cases), 4) if top_cases else 0.0,
        "recall_at_5": 1.0 if matches else 0.0,
        "top_cases": top_cases,
    }


def build_reasoning_chain(
    facts: dict[str, Any],
    evidence: list[dict[str, Any]],
    reference_audit: dict[str, Any],
    case_metrics: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    statutes = [item for item in evidence if item.get("document_type") == "statute"]
    cases = [item for item in evidence if item.get("document_type") == "case"]
    return [
        {
            "id": "facts",
            "title": "案情要素抽取",
            "summary": f"识别案由：{facts.get('案由', '未识别')}；争议焦点：{facts.get('争议焦点', '待补充')}",
            "sources": [{"label": "用户提交案情", "type": "case_text"}],
        },
        {
            "id": "statutes",
            "title": "法条匹配与回检",
            "summary": f"召回 {len(statutes)} 条法规证据；法条/案号回检 {reference_audit['verified']}/{reference_audit['total']} 条通过。",
            "sources": [
                {"document_id": item["document_id"], "label": item["title"], "type": "statute"}
                for item in statutes[:5]
            ],
        },
        {
            "id": "cases",
            "title": "类案比对",
            "summary": (
                f"Top5 同案由命中 {case_metrics['same_cause_hits']}/{case_metrics['top5_count']}，"
                f"Recall@5 = {case_metrics['recall_at_5']:.2f}。"
                if case_metrics
                else f"召回 {len(cases)} 条类案证据。"
            ),
            "sources": [
                {"document_id": item["document_id"], "label": item["title"], "type": "case"}
                for item in cases[:5]
            ],
        },
        {
            "id": "conclusion",
            "title": "辅助性结论",
            "summary": "结论仅基于已检索证据生成，不预测裁判结果；具体维权应咨询执业律师。",
            "sources": [],
        },
    ]


def extract_facts(case_text: str) -> dict[str, Any]:
    cause = "合同纠纷"
    if any(word in case_text for word in ["借条", "借款", "利息", "还款"]):
        cause = "民间借贷"
    elif any(word in case_text for word in ["劳动", "公司", "辞退", "解除", "工资"]):
        cause = "劳动争议"
    elif any(word in case_text for word in ["交通事故", "撞", "保险", "医疗费"]):
        cause = "交通事故"
    elif any(word in case_text for word in ["离婚", "抚养", "夫妻", "财产分割"]):
        cause = "婚姻家事"

    pii = []
    pii.extend(re.findall(r"1[3-9]\d{9}", case_text))
    pii.extend(re.findall(r"\d{17}[\dXx]", case_text))
    names = re.findall(r"[\u4e00-\u9fff]{1,3}某", case_text)
    pii.extend(names[:5])

    return {
        "案由": cause,
        "主体": "系统根据文本初步识别，当事人信息请人工核对",
        "核心事实": case_text[:160] + ("..." if len(case_text) > 160 else ""),
        "诉求": infer_claim(case_text),
        "证据": infer_evidence(case_text),
        "争议焦点": infer_focus(case_text, cause),
        "疑似敏感信息": list(dict.fromkeys(pii)),
    }


def infer_claim(case_text: str) -> str:
    if "尾款" in case_text or "货款" in case_text:
        return "要求支付欠付货款、尾款并承担违约责任"
    if "还款" in case_text or "借款" in case_text:
        return "要求返还借款本金并处理利息或逾期损失"
    if "辞退" in case_text or "解除" in case_text:
        return "要求确认解除是否合法并主张赔偿或补偿"
    if "赔偿" in case_text or "医疗费" in case_text:
        return "要求赔偿合理损失"
    return "请求系统根据案情和证据进行辅助分析"


def infer_evidence(case_text: str) -> str:
    evidence_terms = ["合同", "借条", "转账", "聊天记录", "发货单", "签收", "工资单", "考勤", "事故认定书", "医疗费票据"]
    found = [term for term in evidence_terms if term in case_text]
    return "、".join(found) if found else "需补充合同、付款凭证、沟通记录等证据"


def infer_focus(case_text: str, cause: str) -> str:
    focuses = {
        "合同纠纷": "合同是否成立有效、是否完成交付、是否构成违约、违约责任范围",
        "民间借贷": "借贷合意是否成立、款项是否实际交付、利息或逾期损失如何处理",
        "劳动争议": "解除依据是否充分、程序是否合法、赔偿或补偿如何计算",
        "交通事故": "事故责任比例、损失凭证真实性、保险责任与个人责任范围",
        "婚姻家事": "共同财产范围、出资性质、子女权益和分割比例",
    }
    return focuses.get(cause, "事实是否清楚、证据是否充分、法律依据是否匹配")


def format_evidence(evidence: list[dict[str, Any]]) -> str:
    documents = evidence_documents(evidence)
    lines = []
    for index, document in enumerate(documents, start=1):
        meta = document.metadata
        lines.append(
            f"[{index}] 来源：{meta.get('title')}；类型：{meta.get('document_type')}；案由：{meta.get('case_cause')}；"
            f"地域：{meta.get('region')}；年份：{meta.get('year')}；原文：{document.page_content}"
        )
    return "\n".join(lines)


def evidence_documents(evidence: list[dict[str, Any]]):
    from langchain_core.documents import Document

    documents = []
    for item in evidence:
        documents.append(
            Document(
                page_content=item["content"],
                metadata={
                    "chunk_id": item.get("chunk_id"),
                    "document_id": item.get("document_id"),
                    "title": item.get("title"),
                    "document_type": item.get("document_type"),
                    "case_cause": item.get("case_cause"),
                    "region": item.get("region"),
                    "year": item.get("year"),
                    "source_name": item.get("source_name"),
                    "relevance_score": item.get("relevance_score"),
                },
            )
        )
    return documents


def build_prompt(case_text: str, facts: dict[str, Any], evidence: list[dict[str, Any]]) -> str:
    refs = format_evidence(evidence)
    import json

    return f"""
你是一个法律案例 RAG 辅助分析系统。请严格基于“知识库证据”回答，不要编造不存在的案例、法条或事实。
如果证据不足，请明确说“现有知识库证据不足”，并说明还需要补充哪些资料。
回答必须使用中文 Markdown，尽量在关键结论后标注引用编号，例如 [1] [2]。
每一处《法律名称》第X条或案号都必须能在“知识库证据”原文中逐字找到；查无此条时必须明确说“查无此条，不能作为依据”，不得猜测或补写。
检索到已废止、已修订法规时，必须提示其时效状态和日期。
禁止承诺胜诉，禁止预测裁判结果为确定性结论，禁止把结果表达为正式法律意见或法院结论。

案情：
{case_text}

结构化要素：
{json.dumps(facts, ensure_ascii=False, indent=2)}

证据：
{refs}

请输出：案情摘要、争议焦点、法律依据、类案参考、推理分析、风险提示。
"""


def _qwen_chat(messages: list[dict[str, str]], *, max_tokens: int) -> tuple[str | None, str | None]:
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not USE_LLM or not api_key:
        if USE_LLM and not api_key:
            return None, "已开启 USE_LLM，但未配置 DASHSCOPE_API_KEY，系统已降级为本地模板生成。"
        return None, None
    try:
        import dashscope

        if DASHSCOPE_BASE_HTTP_API_URL:
            dashscope.base_http_api_url = DASHSCOPE_BASE_HTTP_API_URL
        response = dashscope.Generation.call(
            model=db.get_config("chat_model", CHAT_MODEL_NAME),
            messages=messages,
            api_key=api_key,
            result_format="message",
            temperature=0.1,
            max_tokens=max_tokens,
            timeout=DASHSCOPE_TIMEOUT,
        )
        status_code = int(getattr(response, "status_code", 500) or 500)
        if status_code != 200:
            code = str(getattr(response, "code", "") or "unknown_error")
            message = str(getattr(response, "message", "") or "千问未返回有效结果")
            logger.warning("DashScope request failed: status=%s code=%s message=%s", status_code, code, message)
            return None, f"千问调用未成功（{code}）：{message}"
        output = getattr(response, "output", {}) or {}
        choices = output.get("choices") or []
        content = ((choices[0].get("message") or {}).get("content") if choices else "") or ""
        if not content.strip():
            return None, "千问未返回有效文本，已降级为本地知识库摘要。"
        return content.strip(), None
    except Exception as exc:
        logger.warning("DashScope request failed; falling back to local result.", exc_info=True)
        return None, f"千问服务连接失败，已降级为本地知识库摘要：{exc.__class__.__name__}"


def run_langchain_rag(case_text: str, facts: dict[str, Any], evidence: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    return _qwen_chat(
        [
            {
                "role": "system",
                "content": (
                    "你是法律案例 RAG 辅助分析系统。只能依据提供的知识库证据回答；"
                    "证据不足时必须明确说明，不得编造法条、案例或事实。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"案情：\n{case_text}\n\n"
                    f"结构化要素：\n{json.dumps(facts, ensure_ascii=False, indent=2)}\n\n"
                    f"知识库证据：\n{format_evidence(evidence)}\n\n"
                    "请输出中文 Markdown，包含：案情摘要、争议焦点、法律依据、类案参考、推理分析、风险提示。"
                    "关键判断后标注已有引用编号。禁止承诺胜诉，禁止作为正式法律意见。"
                ),
            },
        ],
        max_tokens=1400,
    )


def render_local_result(case_text: str, facts: dict[str, Any], evidence: list[dict[str, Any]]) -> str:
    refs = " ".join(f"[{index}]" for index, _ in enumerate(evidence, start=1))
    law_refs = [item for item in evidence if item["document_type"] == "statute"]
    case_refs = [item for item in evidence if item["document_type"] == "case"]
    law_text = law_refs[0]["snippet"] if law_refs else "当前证据中未检索到直接法规条文，应补充更明确的法律依据。"
    case_text_ref = case_refs[0]["snippet"] if case_refs else "当前证据中类案不足，只能进行谨慎分析。"
    return f"""## 案情摘要
{facts['核心事实']}

## 争议焦点
{facts['争议焦点']}

## 法律依据
检索到的法规或规则提示：{law_text} {refs}

## 类案参考
相似案例显示：{case_text_ref} {refs}

## 推理分析
从现有事实看，本案首先需要确认基础法律关系是否成立，其次核对关键证据是否能够证明履行、违约或损害结果。若用户提供的合同、转账、聊天记录、签收记录等证据能够相互印证，可作为进一步分析的材料。系统结论仅基于当前知识库证据生成，不能替代律师或法院判断。

## 风险提示
证据不足、事实缺失、对方抗辩和法规版本差异都可能影响最终结果。请避免将本分析理解为确定裁判结论。{LEGAL_DISCLAIMER}"""


def render_insufficient_result(case_text: str, facts: dict[str, Any], evidence: list[dict[str, Any]]) -> str:
    refs = " ".join(f"[{index}]" for index, _ in enumerate(evidence, start=1))
    reference_text = f"可参考的有限证据：{refs}" if refs else "当前没有检索到可引用的案例或法规片段。"
    return f"""## 案情摘要
{facts.get('核心事实') or case_text[:160]}

## 争议焦点
{facts.get('争议焦点', '现有事实不足以稳定识别争议焦点。')}

## 法律依据
现有知识库证据不足，暂不能形成可靠的法律依据归纳。{reference_text}

## 类案参考
现有知识库证据不足，暂不能给出稳定的类案参考。

## 推理分析
由于检索证据数量不足或相关度偏低，系统仅能提示需要补充材料，不能据此作出倾向性判断。建议补充合同、付款凭证、沟通记录、履行记录、争议发生时间和对方抗辩材料。

## 风险提示
本结果为证据不足时的降级展示，仅供教学和辅助检索，不构成法律意见或裁判结论。{LEGAL_DISCLAIMER}"""


def validate_result(result: str, evidence: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    warnings: list[str] = []
    if not evidence:
        warnings.append("现有知识库证据不足：未召回可引用片段。")
    elif max(float(item.get("relevance_score", 0.0)) for item in evidence) < 0.08:
        warnings.append("现有知识库证据不足：最高相关度偏低。")

    citation_numbers = {int(item) for item in re.findall(r"\[(\d+)\]", result)}
    expected_numbers = set(range(1, len(evidence) + 1))
    if evidence and not citation_numbers:
        warnings.append("引用完整性校验提示：推理正文未标注引用编号。")
    elif citation_numbers and not citation_numbers.issubset(expected_numbers):
        warnings.append("引用完整性校验提示：正文存在未召回证据对应的引用编号。")

    if any(term in result for term in ABSOLUTE_TERMS):
        warnings.append("绝对化表述校验提示：结果中存在过强结论，应谨慎表述。")
    return not warnings, warnings


def append_validation_warnings(result: str, warnings: list[str]) -> str:
    if not warnings:
        return result
    lines = "\n".join(f"- {warning}" for warning in warnings)
    return f"{result}\n\n## 校验提示\n{lines}"


def generate_reasoning(
    case_text: str,
    facts: dict[str, Any],
    evidence: list[dict[str, Any]],
    case_metrics: dict[str, Any] | None = None,
) -> tuple[str, int, dict[str, Any]]:
    started = time.time()
    diagnostics: list[str] = []
    if not evidence:
        result = render_insufficient_result(case_text, facts, evidence)
    else:
        llm_result, llm_warning = run_langchain_rag(case_text, facts, evidence)
        if llm_warning:
            diagnostics.append(llm_warning)
        result = llm_result or render_local_result(case_text, facts, evidence)
    reference_audit = audit_legal_references(result)
    result = redact_unverified_references(result, reference_audit)
    passed, warnings = validate_result(result, evidence)
    if reference_audit["unverified"]:
        invalid = [item["reference"] for item in reference_audit["items"] if not item["verified"]]
        warnings.append(f"法条/案号幻觉检测：{'、'.join(invalid)} 未通过知识库回检，已标记为查无此条/案号。")
    if reference_audit["total"]:
        accuracy = reference_audit["citation_accuracy"]
        warnings.append(f"法条/案号引用准确率：{accuracy:.2f}%（{reference_audit['verified']}/{reference_audit['total']}）。")
    else:
        warnings.append("法条/案号引用准确率：本次结果未出现可核验的法条号或案号。")
    warnings.extend(validity_notices(evidence))
    warnings.extend(diagnostics)
    result = append_validation_warnings(result, warnings)
    latency_ms = int((time.time() - started) * 1000)
    return result, latency_ms, {
        "passed": passed and reference_audit["passed"] and not diagnostics,
        "warnings": list(dict.fromkeys(warnings)),
        "reference_audit": reference_audit,
        "validity_notices": validity_notices(evidence),
        "reasoning_chain": build_reasoning_chain(facts, evidence, reference_audit, case_metrics),
        "case_retrieval_metrics": case_metrics or {},
    }


def render_local_assistant_reply(question: str, evidence: list[dict[str, Any]]) -> str:
    if not evidence:
        return (
            "我暂时没有在当前知识库中找到足够相关的依据。"
            "你可以补充争议类型、关键事实、时间节点或已有证据，我会继续帮你梳理。"
        )

    first = evidence[0]
    answer = (
        f"针对“{question}”，知识库中最相关的材料是《{first['title']}》。"
        f"其中提到：{first['snippet']}"
    )
    if len(evidence) > 1:
        answer += f"\n\n另外还检索到 {len(evidence) - 1} 条相关材料，可在下方“参考依据”中查看。"
    answer += f"\n\n{LEGAL_DISCLAIMER}"
    return answer


def select_assistant_evidence(evidence: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    qualified = [
        item
        for item in evidence
        if float(item.get("relevance_score") or 0.0) >= ASSISTANT_MIN_RELEVANCE_SCORE
    ]
    if qualified:
        return qualified, []
    if evidence:
        best_score = max(float(item.get("relevance_score") or 0.0) for item in evidence)
        return [], [
            f"检索结果相关度不足（最高 {best_score:.2f}，回答阈值 {ASSISTANT_MIN_RELEVANCE_SCORE:.2f}），系统未采用低相关材料作答。"
        ]
    return [], ["现有知识库证据不足。"]


def run_assistant_llm(
    question: str,
    evidence: list[dict[str, Any]],
    history: list[dict[str, str]] | None = None,
) -> tuple[str | None, str | None]:
    history_text = "\n".join(
        f"{item['role']}：{item['content']}" for item in (history or []) if item.get("content")
    ) or "无"
    return _qwen_chat(
        [
            {
                "role": "system",
                "content": (
                    "你是法律智能小助手。只能依据提供的知识库证据回答，用清晰、克制的中文给出初步分析。"
                    "证据不足时必须直接说明，不得编造法条、案例或事实，不得承诺结果，也不得作为正式法律意见。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"用户问题：\n{question}\n\n"
                    f"最近对话：\n{history_text}\n\n"
                    f"知识库证据：\n{format_evidence(evidence)}\n\n"
                    "请先直接回答用户问题，再用“建议补充”列出仍需核实的事实或材料。"
                    "回答控制在 350 字以内，不要使用未提供的引用编号。"
                ),
            },
        ],
        max_tokens=700,
    )


def generate_assistant_reply(
    question: str,
    evidence: list[dict[str, Any]],
    history: list[dict[str, str]] | None = None,
) -> tuple[str, int, dict[str, Any]]:
    started = time.time()
    diagnostics: list[str] = []
    question_audit = audit_legal_references(question)
    unknown_question_refs = [item["reference"] for item in question_audit["items"] if not item["verified"]]
    if unknown_question_refs:
        answer = (
            f"经知识库法条/案号回检，未找到“{'、'.join(unknown_question_refs)}”。"
            "查无此条/案号时，系统不会据此编造法律依据。请核对法条名称、条号或案号后再查询。"
            f"\n\n{LEGAL_DISCLAIMER}"
        )
        diagnostics.append(f"法条/案号回检未通过：{'、'.join(unknown_question_refs)}。")
    elif not evidence:
        answer = (
            "我暂时没有在当前知识库中找到足够相关的依据，因此不会调用大模型补写法律结论。"
            "请补充争议类型、关键事实、时间节点或已有证据后再查询。"
            f"\n\n{LEGAL_DISCLAIMER}"
        )
        diagnostics.append("现有知识库证据不足，已跳过千问调用。")
    else:
        llm_reply, llm_warning = run_assistant_llm(question, evidence, history)
        if llm_warning:
            diagnostics.append(llm_warning)
        answer = llm_reply or render_local_assistant_reply(question, evidence)
    answer_audit = audit_legal_references(answer)
    answer = redact_unverified_references(answer, answer_audit)
    if answer_audit["unverified"]:
        diagnostics.append("回答中的法条/案号存在未通过回检项，系统已移除。")
    diagnostics.extend(validity_notices(evidence))
    latency_ms = int((time.time() - started) * 1000)
    return answer, latency_ms, {
        "warnings": list(dict.fromkeys(diagnostics)),
        "reference_audit": answer_audit,
        "question_reference_audit": question_audit,
    }


def retrieval_config() -> str:
    return json.dumps(
        {
            "top_k": db.get_int_config("top_k", TOP_K),
            "keyword_weight": db.get_float_config("keyword_weight", KEYWORD_WEIGHT),
            "vector_weight": db.get_float_config("vector_weight", VECTOR_WEIGHT),
            "chat_model": db.get_config("chat_model", CHAT_MODEL_NAME),
            "embedding_model": db.get_config("embedding_model", EMBEDDING_MODEL_NAME),
            "use_dashscope_embedding": USE_DASHSCOPE_EMBEDDING,
        },
        ensure_ascii=False,
    )
