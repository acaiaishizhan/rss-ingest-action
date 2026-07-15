# -*- coding: utf-8 -*-
import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config
import rss_ingest


@dataclass
class DedupEvalCase:
    case_id: str
    expect_duplicate: bool
    title: str
    summary: str
    keywords: str
    candidates: str
    note: str


CASES: List[DedupEvalCase] = [
    DedupEvalCase(
        case_id="false_same_company_different_product",
        expect_duplicate=False,
        title="安克创新eufy Make发布全球首款消费级UV打印机，众筹破4670万美元",
        summary=(
            "安克创新旗下eufy Make团队推出消费级UV打印机E1，Kickstarter众筹达4670万美元；"
            "文章重点讲喷墨控制板卡、双Y轴架构、环保墨水和量产供应链重构。"
        ),
        keywords="安克创新, eufy Make, UV打印机",
        candidates=(
            "C1: AnkerMake 3D打印机众筹后量产交付遇阻\n"
            "  摘要: 安克旗下AnkerMake早期3D打印机项目在众筹后遇到量产和交付问题，团队复盘供应链管理经验。\n"
            "  关键词: 安克创新, AnkerMake, 3D打印机"
        ),
        note="同一家公司、同样硬件众筹背景，但产品和事件不同。",
    ),
    DedupEvalCase(
        case_id="false_reversal",
        expect_duplicate=False,
        title="黄仁勋未获邀随特朗普访华，NVIDIA中国行程出现变化",
        summary="报道称黄仁勋没有出现在特朗普访华企业代表名单中，NVIDIA相关中国行程存在不确定性。",
        keywords="黄仁勋, NVIDIA, 特朗普访华",
        candidates=(
            "C1: 黄仁勋将随特朗普访华，NVIDIA寻求中国市场突破\n"
            "  摘要: 多方消息称黄仁勋将加入特朗普访华企业代表团，并与中国客户讨论AI芯片供应。\n"
            "  关键词: 黄仁勋, NVIDIA, 特朗普访华"
        ),
        note="同一主体同一议题，但核心事实相反，不能当重复。",
    ),
    DedupEvalCase(
        case_id="false_same_tech_different_event",
        expect_duplicate=False,
        title="AWS WorkSpaces推出遗留桌面迁移功能",
        summary="AWS WorkSpaces新增一组迁移工具，帮助企业把旧版桌面环境迁到云端。",
        keywords="AWS WorkSpaces, 桌面迁移, 云桌面",
        candidates=(
            "C1: Hopper发布z/OS现代化工具，帮助主机应用迁移\n"
            "  摘要: Hopper推出面向IBM z/OS应用的现代化工具，帮助企业迁移和改造主机系统。\n"
            "  关键词: Hopper, z/OS, 迁移工具"
        ),
        note="都是企业迁移/现代化工具，但主体、产品、事件完全不同。",
    ),
    DedupEvalCase(
        case_id="false_same_company_different_update",
        expect_duplicate=False,
        title="OpenAI发布ChatGPT企业管理新功能",
        summary="OpenAI为ChatGPT Enterprise增加管理员控制台、权限审计和数据保留设置。",
        keywords="OpenAI, ChatGPT Enterprise, 管理员控制台",
        candidates=(
            "C1: OpenAI发布ChatGPT实时语音更新\n"
            "  摘要: OpenAI为ChatGPT上线实时语音能力，改进低延迟对话和打断响应体验。\n"
            "  关键词: OpenAI, ChatGPT, 实时语音"
        ),
        note="同公司同产品线，但功能更新不同。",
    ),
    DedupEvalCase(
        case_id="guard_same_vendor_agent_vs_video_model",
        expect_duplicate=False,
        title="NovaAI推出浏览器代理开发套件，支持自动执行网页任务",
        summary="NovaAI发布NovaAgent SDK，面向开发者提供网页导航、表单填写和任务编排接口。",
        keywords="NovaAI, NovaAgent, 浏览器代理",
        candidates=(
            "C1: NovaAI发布新一代视频生成模型\n"
            "  摘要: NovaAI推出NovaVideo 2模型，重点提升长镜头一致性、角色保持和视频渲染速度。\n"
            "  关键词: NovaAI, NovaVideo 2, 视频生成模型"
        ),
        note="同一公司，但一个是代理开发套件，一个是视频模型发布，产品和事件都不同。",
    ),
    DedupEvalCase(
        case_id="guard_same_product_different_feature",
        expect_duplicate=False,
        title="WorkHub Copilot新增费用审计面板",
        summary="WorkHub Copilot上线企业费用审计面板，管理员可以查看部门支出、异常采购和审批记录。",
        keywords="WorkHub Copilot, 费用审计, 企业管理",
        candidates=(
            "C1: WorkHub Copilot加入代码仓库问答功能\n"
            "  摘要: WorkHub Copilot新增代码仓库问答，开发者可以在聊天窗口中查询提交历史和接口用法。\n"
            "  关键词: WorkHub Copilot, 代码问答, 开发工具"
        ),
        note="同一产品名称，但一个是企业费用审计，一个是代码仓库问答，功能更新不同。",
    ),
    DedupEvalCase(
        case_id="guard_market_report_vs_product_launch",
        expect_duplicate=False,
        title="BluePeak报告称大型企业AI软件支出继续增长",
        summary="BluePeak发布市场报告，统计500家企业的AI软件采购预算，并分析安全、客服和办公场景投入。",
        keywords="BluePeak, AI软件支出, 市场报告",
        candidates=(
            "C1: BluePeak发布企业AI采购管理产品\n"
            "  摘要: BluePeak推出采购管理产品SpendPilot，帮助企业审批AI工具订阅并追踪团队使用情况。\n"
            "  关键词: BluePeak, SpendPilot, 企业AI采购"
        ),
        note="同公司且都谈企业AI支出，但一个是市场报告，一个是产品发布。",
    ),
    DedupEvalCase(
        case_id="guard_concept_article_vs_version_release",
        expect_duplicate=False,
        title="开发者社区讨论上下文缓存如何降低推理成本",
        summary="文章解释上下文缓存的基本概念、适用场景和计费影响，没有涉及具体产品版本发布。",
        keywords="上下文缓存, 推理成本, 概念解释",
        candidates=(
            "C1: TensorDesk 2.1发布上下文缓存功能\n"
            "  摘要: TensorDesk发布2.1版本，新增上下文缓存开关、缓存命中率指标和团队级配额设置。\n"
            "  关键词: TensorDesk 2.1, 上下文缓存, 版本发布"
        ),
        note="同一概念关键词，但概念解释文章和具体版本发布不是同一新闻。",
    ),
    DedupEvalCase(
        case_id="guard_security_disclosure_vs_fix",
        expect_duplicate=False,
        title="研究人员披露VectorBridge连接器存在权限绕过漏洞",
        summary="安全团队披露VectorBridge连接器漏洞，攻击者可能绕过项目权限读取部分索引配置。",
        keywords="VectorBridge, 权限绕过, 漏洞披露",
        candidates=(
            "C1: VectorBridge发布补丁修复连接器权限漏洞\n"
            "  摘要: VectorBridge发布安全补丁，修复连接器权限绕过问题，并要求管理员升级到最新版本。\n"
            "  关键词: VectorBridge, 安全补丁, 权限绕过"
        ),
        note="同一漏洞主题，但披露和补丁修复是两个阶段。",
    ),
    DedupEvalCase(
        case_id="true_same_funding",
        expect_duplicate=True,
        title="Anthropic完成20亿美元融资，估值升至600亿美元",
        summary="Anthropic完成新一轮20亿美元融资，公司估值达到600亿美元，投资方包括多家机构。",
        keywords="Anthropic, 融资, 估值",
        candidates=(
            "C1: Anthropic据称获20亿美元新融资，估值达600亿美元\n"
            "  摘要: Anthropic完成20亿美元融资，估值达到600亿美元，资金将用于模型研发和商业扩张。\n"
            "  关键词: Anthropic, 20亿美元融资, 600亿美元估值"
        ),
        note="同一公司、同一金额、同一估值，应判重复。",
    ),
    DedupEvalCase(
        case_id="true_same_product_release",
        expect_duplicate=True,
        title="Google发布Gemini 3，多模态和推理能力升级",
        summary="Google发布Gemini 3模型，强调多模态输入、复杂推理和代码能力提升。",
        keywords="Google, Gemini 3, 多模态",
        candidates=(
            "C1: 谷歌推出Gemini 3，强化多模态推理与编程能力\n"
            "  摘要: Google正式推出Gemini 3，主打多模态理解、复杂推理和代码生成能力升级。\n"
            "  关键词: Google, Gemini 3, 推理模型"
        ),
        note="同一公司、同一模型、同一发布事件。",
    ),
    DedupEvalCase(
        case_id="false_rumor_vs_confirmation",
        expect_duplicate=False,
        title="Stability AI确认完成新融资，资金已到账",
        summary="Stability AI官方确认已完成新一轮融资，投资协议签署完成，资金将用于视频模型研发。",
        keywords="Stability AI, 融资, 视频模型",
        candidates=(
            "C1: Stability AI被曝正洽谈新融资\n"
            "  摘要: 知情人士称Stability AI正在与投资方洽谈新融资，金额和条款尚未最终确定。\n"
            "  关键词: Stability AI, 融资传闻, 视频模型"
        ),
        note="传闻和官方确认是不同阶段，不能只因主体和融资主题相同就去重。",
    ),
    DedupEvalCase(
        case_id="false_preview_vs_launch",
        expect_duplicate=False,
        title="Runway正式上线Gen-4视频模型，向付费用户开放",
        summary="Runway宣布Gen-4视频模型正式上线，付费用户今天起可在产品中调用。",
        keywords="Runway, Gen-4, 视频模型",
        candidates=(
            "C1: Runway预告Gen-4视频模型，将在未来几周发布\n"
            "  摘要: Runway展示Gen-4视频模型预览，称该模型仍在内测，计划未来几周向用户开放。\n"
            "  关键词: Runway, Gen-4, 视频模型预告"
        ),
        note="预告和正式上线是不同新闻阶段。",
    ),
    DedupEvalCase(
        case_id="false_investigation_vs_penalty",
        expect_duplicate=False,
        title="欧盟对Mistral开出AI数据合规罚单",
        summary="欧盟监管机构结束调查，因训练数据披露不足对Mistral处以罚款，并要求限期整改。",
        keywords="Mistral, 欧盟监管, 数据合规",
        candidates=(
            "C1: 欧盟启动对Mistral训练数据合规调查\n"
            "  摘要: 欧盟监管机构宣布调查Mistral是否充分披露AI训练数据来源，目前尚未作出处罚决定。\n"
            "  关键词: Mistral, 欧盟监管, 数据合规调查"
        ),
        note="监管调查和处罚落地是不同阶段。",
    ),
    DedupEvalCase(
        case_id="false_paper_vs_open_source",
        expect_duplicate=False,
        title="Sakana AI开源新型模型训练代码",
        summary="Sakana AI发布模型训练代码和权重，开发者可以复现实验并在本地运行。",
        keywords="Sakana AI, 开源代码, 模型训练",
        candidates=(
            "C1: Sakana AI发表新型模型训练论文\n"
            "  摘要: Sakana AI发布论文介绍一种新的模型训练方法，展示实验结果但尚未开放代码。\n"
            "  关键词: Sakana AI, 论文, 模型训练"
        ),
        note="论文发布和代码开源不是同一新闻。",
    ),
    DedupEvalCase(
        case_id="false_vulnerability_vs_patch",
        expect_duplicate=False,
        title="LangChain发布补丁修复提示注入漏洞",
        summary="LangChain发布安全更新，修复此前披露的提示注入漏洞，并建议用户升级。",
        keywords="LangChain, 提示注入, 安全补丁",
        candidates=(
            "C1: 研究人员披露LangChain提示注入漏洞\n"
            "  摘要: 安全研究人员披露LangChain存在提示注入漏洞，攻击者可能绕过部分应用限制。\n"
            "  关键词: LangChain, 提示注入, 漏洞披露"
        ),
        note="漏洞披露和补丁修复是不同进展。",
    ),
    DedupEvalCase(
        case_id="true_same_security_patch",
        expect_duplicate=True,
        title="LangChain修复提示注入漏洞，建议用户立即升级",
        summary="LangChain发布安全补丁，修复提示注入漏洞，并提醒开发者升级到最新版本。",
        keywords="LangChain, 提示注入, 安全补丁",
        candidates=(
            "C1: LangChain发布安全更新修补提示注入问题\n"
            "  摘要: LangChain推出安全更新，修复提示注入漏洞，官方建议用户尽快升级。\n"
            "  关键词: LangChain, 安全更新, 提示注入"
        ),
        note="同一软件、同一漏洞、同一修复动作，应判重复。",
    ),
]


def run_case(case: DedupEvalCase, provider: str) -> Dict[str, Any]:
    prompt = rss_ingest._load_dedup_prompt()
    user_content = (
        f"# 新文章\n"
        f"标题: {case.title}\n"
        f"摘要: {case.summary}\n\n"
        f"# 已入库文章\n{rss_ingest.strip_dedup_keyword_lines(case.candidates)}"
    )
    article = {"title": case.title, "content": user_content, "link": "", "source": "dedup-eval"}
    provider_name = rss_ingest.normalize_provider_name(provider or config.LLM_PROVIDER)
    model = rss_ingest.provider_model_for_stage(provider_name, "screen")
    result = rss_ingest.analyze_with_provider_prompt(article, provider_name, prompt, model)
    actual = result.get("is_duplicate") is True
    return {
        "case_id": case.case_id,
        "expected": case.expect_duplicate,
        "actual": actual,
        "pass": actual == case.expect_duplicate,
        "note": case.note,
        "raw": result,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run read-only LLM dedup prompt regression cases.")
    parser.add_argument("--provider", default=os.getenv("DEDUP_EVAL_PROVIDER", ""), help="LLM provider; defaults to LLM_PROVIDER.")
    parser.add_argument("--case", default="", help="Run one case_id only.")
    args = parser.parse_args()

    cases = [c for c in CASES if not args.case or c.case_id == args.case]
    if not cases:
        print(f"no case matched: {args.case}")
        return 2

    failures = 0
    results = []
    for case in cases:
        result = run_case(case, args.provider)
        results.append(result)
        status = "PASS" if result["pass"] else "FAIL"
        print(f"{status} {case.case_id}: expected={result['expected']} actual={result['actual']}")
        print(json.dumps(result["raw"], ensure_ascii=False, indent=2))
        print()
        if not result["pass"]:
            failures += 1

    print(f"summary: {len(cases) - failures}/{len(cases)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
