# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import csv
import hashlib
import hmac
import json
import mimetypes
import os
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import formatdate
from pathlib import Path
from typing import Any, Optional

# APP_KEY = os.getenv("APP_KEY", "1001300033")
# SECRET_KEY = os.getenv("SECRET_KEY", "24e74daf74124b0b96c9cb113162a976")
# URL = os.getenv("GATEWAY_URL", "https://192.168.0.213:18300/ai-inference-gateway/predict")
# COMPONENT_CODE = os.getenv("COMPONENT_CODE", "04100567")
# MODEL = os.getenv("MODEL", "Qwen3-235B-A22B-w8a8")

# APP_KEY = os.getenv("APP_KEY", "1001300035")
# SECRET_KEY = os.getenv("SECRET_KEY", "68bbe87e123b40089c4196a30b435bbc")
# URL = os.getenv("GATEWAY_URL", "https://10.10.65.213:18300/ai-inference-gateway/predict")
# COMPONENT_CODE = os.getenv("COMPONENT_CODE", "04101188")
# MODEL = os.getenv("MODEL", "Qwen3-235B-A22B-w8a8")
# 智学
APP_KEY = os.getenv("APP_KEY", "1001300033")
SECRET_KEY = os.getenv("SECRET_KEY", "24e74daf74124b0b96c9cb113162a976")
URL = os.getenv("GATEWAY_URL", "https://10.10.65.213:18300/ai-inference-gateway/predict")
COMPONENT_CODE = os.getenv("COMPONENT_CODE", "04100567")
MODEL = os.getenv("MODEL", "Qwen3-235B-A22B-w8a8")

# DEFAULT_CONCURRENCY_LEVELS = [2,4,8,12,16,20,22,24,26,28,30,32,34,36,38,40]
DEFAULT_CONCURRENCY_LEVELS = [10,20,30,40,50,60,70,80,90,100,110,120]
DEFAULT_SYSTEM_PROMPT = "你是一个严谨的测试助手，请根据用户要求给出清晰、可验证的回答。"
DEFAULT_USER_PROMPT = """
# 角色定义
资深电网国企面试官，擅长从候选人**真实工作经历、业务成果、解决问题的方法**中提炼考察点，尤其擅长出"压力场景+短板验证"类行为面试题。

# 任务
基于下方提供的候选人简历数据、候选人优劣势，生成 **{question_count}** 道行为面试题及对应的STAR标准答案。
- 强制要求：必须生成 **{question_count}** 道题目
- 题目之间不得重复（场景、能力、压力点不重复）
- 绝对红线：所有题目生成完毕前，禁止输出 stop、end 或提前截断，必须输出恰好 {question_count} 个 questions 对象
# 核心原则

## 出题原则
1. **场景真实**：必须来自电网变电运维/项目建设/班组管理真实矛盾（如：工期压减、厂家推诿、骨干被抽走、上级要求不停电处理）
2. **压力前置**：题干中明确1-2个不可绕开的卡点（技术争议、资源短缺、跨部门扯皮、安全风险）
3. **角色锁定**：明确候选人是"牵头人/负责人/班组长"，不能模糊
4. **任务量化**：要求回答中体现硬性指标（如：按期送电、零违章、缺陷消除率100%）
5. **短板验证**：劣势类题目必须针对候选人真实劣势
6. **禁止空话**：答题中不得出现"加强管理、提高重视、完善机制"等无具体动作的描述

## 答题原则
请严格按照STAR法则作答，结构完整、逻辑闭环、贴合电力运维/项目管理/班组管理真实场景，杜绝空话套话。
1. **S（情境）**：清晰交代事件背景、工作场景、任务目标、时间节点、现场存在的核心矛盾与压力难点
2. **T（任务）**：明确我本人的岗位职责、牵头角色、核心攻坚任务，以及需要解决的关键问题、必须达成的硬性指标
3. **A（行动）**：重点展开具体做法、统筹思路、技术举措、协调动作、管控手段，分条写实、落地可追溯，突出个人主导性、决策性、创新性，聚焦解决难点、化解矛盾、突破瓶颈的关键动作
4. **R（结果）**：量化工作成效、解决的实际问题、取得的成果，同时补充复盘总结、经验沉淀、长效提升

**答题要求**：语言精炼、干部口吻、实战写实、突出个人能力、突出解决复杂问题的能力。

# 出题策略（动态分配，严格按此逻辑）

## 综合类问题类型:
专岗经验，管理经验，攻坚经历，激励星级，荣誉奖项，工作绩效，任职不稳，经历断层，专业匹配，业绩不明，惩处记录，家庭稳定，沟通协调，原则底线，行业前瞻，顶层设计，抗压能力，风险决策，系统思维，闭环管理，学习能力，专业钻研，数据治理，合规管控，文字综合，标准执行
（当综合类题目数量超过类型总数时，允许循环使用类型，但每道题目的考察技能点和压力场景必须不重复）

## 输入数据
- 记优势数量： S_len = {strengths_count}
- 劣势数量：W_len = {weakness_count}
- 需要生成题目总数 = {question_count}

## 分配规则

### 第1步：优势类题目数量
- strengths_count = min(S_len, 2)  （最多2道）
- 从候选人优势中按顺序选取前 strengths_count 个优势，每个优势出1道题

### 第2步：劣势类题目数量
- weaknesses_count = min(W_len, 2)  （最多2道）
- 若 W_len >= 1，从候选人劣势中按顺序选取前 weaknesses_count 个劣势，每个劣势出1道题
- 若 W_len = 0，则 weaknesses_count = 0

### 第3步：综合类题目数量（补齐）
- capabilities_count = {question_count} - strengths_count - weaknesses_count
- 若 capabilities_count > 0，从综合类问题类型中按随机选取 capabilities_count 个类型，每个类型出1道题
- 若 capabilities_count = 0，则不输出该类题目

### 第4步：题目顺序
- 输出顺序：先所有优势题 → 再所有劣势题 → 最后综合类问题中抽取
- 同类题目中按列表顺序排列

# 输出JSON结构（必须严格遵守）
```json
{{
  "questions": [
    {{
      "question": "完整问句",
      "skills": ["从综合类问题类型中选取", "最多2个"],
      "description": "考察点描述",
      "answer": "回答示例"
    }}
  ],
  "total_count": {question_count}
}}

# Few-shot示例（以下示例均为"输入简历数据 -> 对应输出"，用于学习风险判断边界与表述方式）

## 岗位信息：
- 通用岗位：通用岗位
- 岗位描述：面向系统内招聘

# 候选人简历数据
- 应聘公司/部门/岗位: 云南电网有限责任公司临沧供电局/规划建设管理中心（质量监督项目站）综合业务室/技术质量管理专责
- 出生日期: 1989-06-22
- 籍贯: 临沧市云县
- 目前单位/岗位: 云南电网有限责任公司临沧供电局临沧云县供电局/高级网络安全及信息运维管理员
- 教育: 大学本科，昆明理工大学，电气工程及其自动化*
- 工作年限: 14年
- 职称证书: 工程师（中级）、助理工程师（助理级）
- 职称等级（最高）: 中级
- 技能证书: 技能等级证书（三级(高级工)）、职业技能等级证书（三级(高级工)）、职业技能等级证书（四级(中级工)）
- 外语语种及水平: 汉语(中文)其他
- 入党时间: 2018/11/24，政治面貌: 中国共产党党员
- 近三年业绩: 本人自参加工作以来，从事过基建项目造价管理、生产项目管理、配电工、配网生产管理、检修试验工、网络安全管理及国家级重大项目策划等，在从事多项岗位中，均能快速适应并自主承接各项工作。       自2021年11月至2024年3月期间，在从事计划生产部检修工时，及时取得各项准入证书，并根据要求，逐步获得中级工、高级工岗位胜任能力资格。在取得相应证书后，及时投入现场工作，积极开展配电自动化终端定值更改、专业巡维等工作，对离线设备及时处理，确保FTU终端在线率达标。完成我局配电自动化终端在线率，配电自动化终端故障隔离准确率100%。为有效提升我局供电可靠性，确保故障时能有效正确隔离故障点，及时开展配电自动化终端定值更改、专业巡维等工作，对离线设备及时处理，确保FTU终端在线率达标。2022年，完成配电自动化终端在线率98.85%，较年度指标97.58%优1.27个百分点。配电自动化终端故障隔离准确率100%，节约时户数4102时·户。        2024年3月，因工作需求，借调至营配指挥中心从事网络安全及信息运维工作，在此工作期间，一是将网络安全摆在和电网安全同等重要位置，建立"全域全流程、省地县一体、专业加联动"的网络安全管理体系，筑牢网络安全防线，强化网络安全"人、物、管理、环境"的本质安全能力建设，全面提升网络安全实战防护能力和防护水平，确保网络安全运行状态"零风险"；二是全面承接上级科数中心网络安全工作要求，先后组织完成全员网络安全知识培训3次及全员网络安全责任书签订237份，处置修复313个网络安全漏洞及486份终端设备维护工单闭环，完成弱口令整改162项，保证了全局信息网络的安全稳定运行，未发生Ⅳ级及以上网络信息安全事件；三是积极推动数字化应用和科技创新工作，坚持技术创新与安全生产应用相结合，在安全生产业务升级、工器具研制等方面申报职工创新项目8个，总投资28.4万元；四是积极推动人工智能、RPA、云景平台、智搜+等数字化专业技术推广应用，其中，推广应用RPA流程机器人4个，自主设计制作云景平台应用场景12个，为提升数字化应用效能奠定了有力支撑；五是积极推动通信光纤管控难、实施难、运维推动难的堵点难点问题根治，完成12条通信光缆日常运维，完成2025年通信光缆日常维护项目申报，申报项目资金12万元，为下一步我局通信光缆巡视、维护奠定基础；六是积极推进2024护网实战攻防演习工作，编制我局"HW2024"专项行动（网络攻防实战演习）备战迎战工作方案、19项任务清单、14项技防措施清单，统筹全局力量做好迎战备战各项工作，圆满完成"三不一零"底线和"三个不中断"总体目标。        2025年9月至2026年2月，根据公司安排，到昆明电科院跟班学习，学习期间，积极进取、主动担当，高质量协助科创中心开展重大科技项目策划、实施与管理工作，成效显著。在"智能电网"国家科技重大专项主赛道，主动配合组织各领域专家开展项目可研评审、申报资料审查等关键工作，协同完成国家级科技项目策划申报10项，创公司历史新高；全力配合推进国家级、网级实验室的策划与申报工作，为科创平台建设提供有力支撑；积极牵头组织国家自然科学基金项目的策划、论证与申报工作，此次项目申报实现了云南电网公司在国家自然科学基金赛道上的"零突破"，在此学习结束后，得到云南电网公司科创中心书面表扬。        最后，作为一名党员和电力人，始终牢记"人民电业为人民"的企业宗旨，以高度的政治责任感推动配电自动化项目发展，以高度的执行力不折不扣落实局党委要求，想尽一切办法，克服一切困难，坚持不懈锤炼党性。
- 年度绩效情况: 候选人在2024年的工作业绩为：B。; 候选人在2023年的工作业绩为：A。; 候选人在2022年的工作业绩为：B。; 候选人在2021年的工作业绩为：B。
- 荣誉: 2025-08-31在临沧供电局工会获得临沧供电局2025年"素质杯"数字化应用个人二等奖。; 2025-08-05在云南电网有限责任公司获得云南电网公司2025年职工创新技术集体三等奖。; 2024-06-30在临沧市总工会、临沧供电局获得临沧市供电企业2024年"安康杯"信息与数字化竞赛个人三等奖。; 2024-05-31在云南电网有限责任公司获得云南电网公司2024年度职工技术创新集体三等奖。
- 处分记录：无
- 工作经历: 2025年12月起至今在云南电网公司临沧供电局临沧云县供电局营配指挥中心（运营监控中心）监测分析班担任高级网络安全及信息运维管理员。; 2025年12月至2025年12月，在营配指挥中心（运营监控中心）监测分析及智能作业班担任高级网络安全及信息运维管理员。; 2025年01月至2025年12月，在营配指挥中心（运营监控中心）监测分析及智能作业班担任高级网络安全及信息运维管理员。; 2024年11月至2025年01月，在计划生产部检修试验班担任高级检修试验工。; 2021年09月至2024年11月，在计划生产部检修试验班担任中级检修试验工。; 2020年08月至2021年09月，在计划生产部担任配网检修专责A及配网安全管理专责。; 2018年10月至2020年08月，在安全生产部（应急指挥中心）担任配电管理专责B。; 2017年10月至2018年10月，在担任配电工。; 2014年02月至2017年10月，在生产设备管理部担任科技专责。; 2012年07月至2014年02月，在规划建设部担任工程造价兼统计。
- 项目经历: 无
- 家庭成员：候选人的妻子在临沧市第一中学担任职工。政治面貌为群众（现称普通公民）。; 候选人的长女在易成试验小学担任学生。政治面貌为群众（现称普通公民）。; 候选人的次女在临沧富丽幼儿园担任学生。政治面貌为群众（现称普通公民）。; 候选人的父亲在云县信用合作联社信贷员工作。政治面貌为群众（现称普通公民）。; 候选人的母亲在个体户工作。政治面貌为中国共产党党员。

## 候选人优势：
  专岗经验：在基建、生产、配电、检修及网络安全岗位连续工作14年（2012-2025），完成配电自动化终端在线率提升至98.85%、故障隔离准确率100%、节约时户数4102时·户，对口经验较高。
  岗位层级：担任云南电网有限责任公司临沧供电局临沧云县供电局营配指挥中心监测分析班高级网络安全及信息运维管理员，2025年12月至今负责网络安全、信息运维及数字化应用推广，能力良好。
  攻坚经历：2022年完成配电自动化终端在线率98.85%，较年度指标优1.27个百分点，故障隔离准确率100%，节约时户数4102时·户。2024年牵头推进HW2024专项行动，编制备战迎战方案及19项任务清单，实现全局网络信息安全事件'零风险'。2025年在昆明电科院跟班期间牵头组织国家自然科学基金项目申报，实现云南电网公司'零突破'。
  持证情况：持有工程师（中级）职称及职业技能等级证书（三级(高级工)），具备技术质量管理与网络安全运维相关能力，等级一般。
  荣誉奖项：2024年获临沧市供电企业'安康杯'信息与数字化竞赛个人三等奖（地市级专业荣誉）。2025年获临沧供电局'素质杯'数字化应用个人二等奖（地市级专业荣誉）。2024年、2025年分别获云南电网公司职工技术创新集体三等奖（省公司级专业荣誉）。综合等级为良好。

## 候选人劣势：
  专业匹配：候选人近年主要担任网络安全及信息运维、检修试验工等岗位，虽具备配电自动化终端维护经验，但应聘岗位需技术质量管理全链条经验，缺乏规划、设计、建设全过程质量监督实践，岗位匹配度不足。
对应输出：
{{
  "questions": [
    {
      "question": "你目前主要从事网络安全及信息运维工作，而本次应聘的是规划建设管理中心的技术质量管理专责。请结合你在2024年推动数字化应用或护网演习的经历，谈一谈你是如何将信息化手段应用到传统电网生产业务中的？如果未来在基建工程质量监督中遇到数据孤岛或监管滞后的问题，你会如何借鉴过往的中级/高级工技能与信息化经验来破局？",
      "skills": ["专岗经验", "技职水平"],
      "description": "基于专岗经验与技职水平，考察信息技术与电网业务融合的创新能力及前瞻性破局思维",
      "answer": "2024年借调营配指挥中心期间，面对全局网络安全漏洞频发、终端设备维护工单量大且人工处理效率低下的痛点，以及上级要求将网络安全提升至与电网安全同等重要位置的高压态势，我作为高级网络安全及信息运维管理员，主动牵头建立'全域全流程'管理体系以消除弱口令等安全隐患。在具体执行中，我梳理高频重复业务流程，自主设计并推广应用了4个RPA流程机器人和12个云景平台应用场景；针对通信光缆管控难的问题申报专项资金并制定日常运维机制；同时在'HW2024'实战攻防演习中统筹编制专项方案与技防措施清单，将被动防御转化为主动的数据监测联动处置。最终成功闭环486份终端维护工单，整改162项弱口令，圆满完成'三不一零'底线目标。这一经历让我深刻认识到技术质量监督的核心在于'用数据驱动管理'，若入职新岗位，我将把这种数字化思维引入基建质监，利用信息化平台打通工程进度与质量验收的数据壁垒，实现从'事后检查'向'全过程在线智能监控'的转变。"
    },
    {
      "question": "简历显示你在2025年赴昆明电科院跟班学习期间，协助完成了国家级科技项目的策划申报，实现了公司在国家自然科学基金赛道上的'零突破'。请详细复盘这项重大攻坚任务，在面对高规格、严要求的科研策划时，你作为基层单位派出的跟班人员，是如何克服专业壁垒、协调各方资源并最终达成目标的？",
      "skills": ["攻坚经历", "荣誉奖项"],
      "description": "基于攻坚经历与荣誉奖项，考察在国家级高规格项目中的跨层级协调与突破性执行能力",
      "answer": "2025年9月至2026年2月我在昆明电科院科创中心跟班学习时，正值'智能电网'国家科技重大专项及国家自然科学基金申报的关键期，面临时间紧、任务重且涉及多领域前沿技术交叉的极高要求。我的核心任务是高质量协助开展重大科技项目策划，重点配合专家进行可研评审与资料审查，并牵头推进国家自然科学基金项目的论证申报，力争实现历史性突破。面对专业跨度大的挑战，我主动发挥电气工程及其自动化专业背景与多年基层一线工作经验相结合的优势充当'理论'与'现场'的桥梁，积极对接各领域专家提前梳理申报痛点，协同完成10项国家级科技项目策划的资料打磨；在国家自然科学基金申报中，我牵头组织多轮内部论证，逐字逐句核对技术指标与应用场景以确保契合工程实际。最终我们协同完成10项国家级科技项目申报创公司历史新高，并成功实现国家自然科学基金赛道'零突破'获省公司科创中心书面表扬，这次攻坚经历极大锻炼了我统筹复杂项目、把控核心节点的能力，这正是优秀基建质监专责应对重大工程质量核查和高标准项目验收时不可或缺的核心素养。"
    },
    {
      "question": "你在计划生产部担任检修工时，曾负责配电自动化终端定值更改与离线设备处理，并实现了故障隔离准确率100%。请分享一次在现场巡维或终端消缺过程中，遇到的最棘手的技术难题或突发状况。你是如何快速定位问题、协调资源，并确保不影响整体供电可靠性的？",
      "skills": ["管理经验", "持证情况"],
      "description": "基于专业匹配缺口，考察现场突发技术问题中的快速定位、资源协调与保供电实战能力",
      "answer": "在从事计划生产部检修工期间，随着配网自动化设备增加，部分老旧FTU终端频繁出现离线或定值不匹配现象严重威胁区域供电可靠性，某次极端天气后辖区内多个终端集中告警导致现场排查难度极大。作为现场骨干，我必须迅速查明根本原因并完成定值精准更改与设备消缺，确保终端在线率达标且不引发二次停电或扩大故障范围。我第一时间启动应急响应，利用主站系统数据分析锁定异常点位与通信状态，随后带领团队赶赴现场采取'先保通信、后查本体'策略逐一排查光纤链路、电源模块及主板程序；针对共性问题现场制定了标准化消缺作业指导书规范后续操作，同时与调度端保持实时联动采用旁路代供等方式保障居民用电不受影响。最终不仅迅速恢复了所有终端正常运行，还将2022年配电自动化终端在线率提升至98.85%（优于年度指标1.27个百分点），故障隔离准确率达100%并节约时户数4102时·户，这段扎根现场的排故经验让我对配网设备的物理特性与运行工况有了直观认知，未来在做基建质量监督时我能更敏锐地发现施工安装工艺和设备选型等环节可能埋下的隐患。"
    },
    {
      "question": "你近年来多次获得'素质杯'数字化应用个人二等奖、职工创新技术集体三等奖等荣誉，并在各项工作中保持了较高的绩效评价。这些荣誉背后往往伴随着对现有工作流程的颠覆或优化。请具体讲述一个你主导或深度参与的职工创新项目，谈谈你是如何发现业务痛点的？该创新成果在实际应用中取得了什么成效？如果将该创新思维应用到当前的基建工程质量管控中，你有什么设想？",
      "skills": ["激励星级", "工作绩效"],
      "description": "基于工作绩效与创新荣誉，考察从业务痛点挖掘到成果转化的创新实践能力及思维迁移",
      "answer": "在日常网络安全与信息运维工作中，我发现大量基础台账核对与漏洞通报下发等工作依赖人工流转不仅耗时费力且极易因人为疏忽导致合规风险，同时安全生产业务升级急需轻量化数字化工具支撑。作为创新项目骨干，我立足岗位痛点牵头组建跨专业柔性团队深入调研需求，自主设计了基于云景平台的12个应用场景将分散的台账数据进行结构化整合，主导了从需求分析、原型测试到上线推广的全流程并建立了'发现问题-提出创意-落地验证'的创新孵化机制，进而将此模式复制到工器具研制等领域累计申报了8个职工创新项目。相关成果荣获临沧供电局'素质杯'数字化应用个人二等奖及云南电网职工创新技术集体三等奖，切实为基层减负并提升了数据准确性。对于基建质监而言这种'微创新'思维同样适用，例如可以开发基于移动端的质量缺陷随手拍与自动归类工具，或者利用AI图像识别技术辅助隐蔽工程的影像资料审查，用技术创新倒逼工程质量标准的刚性执行。"
    },
    {
      "question": "你的履历非常丰富，横跨工程造价、配网管理、检修试验、网络安全以及省级科研院所跟班学习等多个板块。虽然这体现了你极强的适应能力，但作为规划建设管理中心的技术质量管理专责，需要长期深耕基建领域。请坦诚谈谈，你频繁跨越不同专业序列的核心驱动力是什么？面对即将到来的基建质监新岗位，你如何保证不再出现'短期适应后再次寻求跨界'的情况，从而确保履职的长期稳定性？",
      "skills": ["任职不稳", "专业匹配"],
      "description": "基于任职不稳的劣势，考察职业规划清晰度、岗位忠诚度及长期扎根的决心",
      "answer": "自2012年参加工作以来我经历了从规划建设部造价岗到生产设备部科技岗再到安监、生技、营配指挥等多岗位的历练，每次岗位变动均是因为组织工作需要或个人在特定阶段遇到了能力瓶颈需要通过新环境拓宽视野。我需要向面试官清晰阐释我的职业发展底层逻辑以证明过往的'多面手'经历并非盲目跳槽而是为了构建复合型知识体系，同时给出令人信服的承诺表明我已找到值得长期奋斗的职业锚点。我的每一次跨界都是围绕'大电网安全与高质量发展'这一主线展开的，懂造价让我具备成本意识，懂配网和检修让我掌握设备全生命周期规律，懂网络安全和科创则赋予我数字化与前瞻性视角；如今我已36岁处于职业生涯的黄金成熟期且家庭稳定（妻子在本地任教父母均在本地），非常渴望将沉淀多年的复合经验在一个核心岗位上生根发芽，而基建质监正是能将工程管理底子、现场设备经验和数字化管理思维完美融合的'集大成'岗位。过去的广度积累是为了今天的深度发力，我深知基建质监工作需要耐得住寂寞守得住底线，已做好长远规划将以此次竞聘为新起点沉下心来钻研基建规程与质量标准致力于成为该领域的专家型专责，绝不会再轻易偏离这条专业深耕的道路。"
    },
    {
      "question": "基建工程质量监督往往需要与施工单位、监理单位以及内部设计部门频繁对接。如果在一次关键节点的隐蔽工程验收中，施工单位以'工期紧、任务重'为由拒绝配合整改，而监理单位也态度暧昧不愿出具不合格报告，作为质监专责，你将如何打破这种'人情世故'的僵局，确保工程质量底线不被突破？",
      "skills": ["沟通协调", "原则底线"],
      "description": "基于多方博弈场景，考察坚持原则的定力、跨部门沟通技巧及解决复杂人际冲突的能力",
      "answer": "面对这种多方利益交织的僵局，我深知基建质监专责不仅是技术的把关人，更是规则的捍卫者。首先，我会保持冷静克制，避免陷入情绪化的争吵，而是将沟通焦点从'人的对立'转移到'客观标准'上。我会立即调取相关施工图纸、国家强制性标准及隐蔽工程验收规范，用确凿的技术数据和规程条款向施工方和监理方说明该缺陷可能引发的长期安全隐患及后期高昂的返工成本，做到'以理服人、以规压阵'。其次，针对监理方态度暧昧的问题，我会依据监理合同及考核管理办法进行严肃约谈，明确其失职可能面临的违约处罚与信用评价降级风险，倒逼其履职尽责。最后，如果现场沟通无效，我将果断启动升级汇报机制，第一时间向分管领导及项目指挥部报告现场情况，申请召开多方协调会，必要时下发停工整改通知书并留存影像证据。我坚信，真正的沟通不是无底线的妥协，而是在坚守质量红线的前提下寻找解决问题的最优路径，只有把规矩立在明处，才能赢得各方长久的尊重与配合。"
    },
    {
      "question": "南方电网当前正大力推进数字化转型，提出从'数据驱动'向'AI赋能'升级。结合你过往在RPA机器人和云景平台的应用经验，你认为当前基建工程质量管理在数字化方面还有哪些短板？如果由你牵头，你会如何规划基建质监业务的智能化升级路径？",
      "skills": ["行业前瞻", "顶层设计"],
      "description": "基于行业趋势与个人数字化特长，考察对基建业务数字化的深度思考及系统性规划能力",
      "answer": "当前基建质监在数字化方面仍存在'重记录轻分析、重结果轻过程'的短板，大量质量验收数据停留在纸质台账或孤立的业务系统中，未能形成指导工程决策的数据资产。结合我过往推广RPA和云景平台的经验，我认为基建质监的智能化升级应分三步走：第一步是'数据同源与底座夯实'，打通基建管控系统、物资系统与现场移动终端的数据链路，推行工程质量缺陷的标准化录入，确保底层数据的真实与鲜活；第二步是'场景驱动与AI赋能'，针对高频痛点开发实用工具，例如利用计算机视觉技术对现场钢筋绑扎、混凝土浇筑等关键工序进行AI自动比对与违规抓拍，将事后抽查变为全天候智能巡检；第三步是'数据反哺与生态协同'，建立供应商与施工单位的工程质量数字画像，将历史质量缺陷数据与招投标及履约评价挂钩，用数据倒逼产业链整体质量提升。我的核心思路是技术必须服务于业务，不盲目追求高大上的概念，而是用数字化手段切实解决基建现场'看不见、管不全、评不准'的老大难问题。"
    },
    {
      "question": "在重大基建项目推进过程中，往往会遇到极端天气、物资延迟或设计变更等突发状况，导致工程进度严重滞后且质量风险骤增。作为技术质量管理专责，当面临上级要求'按期投产'与现场'质量隐患未消除'的尖锐矛盾时，你如何进行风险评估与决策平衡？",
      "skills": ["抗压能力", "风险决策"],
      "description": "基于极端压力场景，考察在多重约束条件下的风险研判、底线思维及综合决策能力",
      "answer": "在'保进度'与'保质量'的尖锐矛盾面前，我始终坚持'安全质量是不可逾越的红线，进度必须建立在合规的基础之上'这一核心原则。面对此类突发状况，我首先会迅速组织技术骨干与监理、施工方开展现场联合勘查，对质量隐患进行分级分类评估，明确哪些是可以通过临时加固或加强监测来管控的'一般风险'，哪些是可能导致设备损毁或人身伤亡的'致命风险'。对于致命风险，我会顶住压力坚决行使质监一票否决权，出具书面风险提示函并向上级详细汇报强行推进的严重后果，用专业的风险评估报告为领导决策提供依据；对于一般风险，我会牵头制定专项过渡方案与应急预案，增加巡视频次并落实旁站监督，在确保受控的前提下配合推进工程节点。同时，我会积极协调设计、物资等部门加快变更审批与物资调配，从源头压缩延误时间。真正的担当不是盲目服从，而是在复杂局面中敢于亮明专业态度，用科学的风险管控手段在合规框架内寻找最优解。"
    },
    {
      "question": "基建工程质量监督不仅需要发现表面的施工缺陷，更需要具备从'事后整改'向'事前预防'转变的能力。结合你在配网检修和网络安全运维中积累的'隐患排查'经验，你将如何构建一套基建工程质量的'事前预警与源头管控'机制？",
      "skills": ["系统思维", "闭环管理"],
      "description": "基于跨专业经验迁移，考察建立长效机制、源头治理及全生命周期质量管控的能力",
      "answer": "无论是网络安全中的漏洞排查还是配网设备的缺陷消缺，核心逻辑都是'防患于未然'。如果将这套经验迁移到基建质监，我认为构建事前预警机制需要抓好三个前置关口：第一是'标准前置'，在工程开工前，我会联合设计、施工方开展图纸会审与质量交底，将以往工程中频发的高频缺陷点转化为本项目的'质量通病防治清单'，让施工方在动工前就明确红线；第二是'准入前置'，借鉴网络安全中设备入网测试的思路，严把材料设备进场关与施工队伍资质审查关，对关键原材料实行见证取样与第三方盲检，坚决将不合格品挡在工地之外；第三是'工序前置'，推行首件工程认可制与样板引路，在全面铺开施工前先做样板，经质监验收达标后再作为后续施工的实物标准。此外，我会建立质量缺陷数据库，定期开展趋势分析，将事后发现的共性问题转化为事前管控的重点指标，真正实现从'救火式整改'向'防火式预防'的管理升级。"
    },
    {
      "question": "在跟班学习或跨部门协作中，你经常需要面对非本专业领域的复杂问题。作为基建质监专责，面对新型建筑材料或前沿施工工艺（如装配式建筑、智能变电站）时，你如何快速跨越知识盲区，确保自己的质量监督'内行管内行'而不被施工方忽悠？",
      "skills": ["学习能力", "专业钻研"],
      "description": "基于新知识获取场景，考察快速学习能力、知识转化能力及保持专业权威的策略",
      "answer": "面对新型材料与前沿工艺，我深知'打铁还需自身硬'，绝不能以'外行'身份敷衍了事。我的破局策略分为三步：首先是'靶向学习，构建框架'，我会第一时间收集该工艺的国家标准、行业规范及典型设计图集，带着现场实际图纸进行对标学习，在最短时间内掌握其核心原理与关键质量控制点；其次是'借力外脑，深度请教'，充分利用我在电科院跟班学习时积累的专家资源网络，主动向科研院所、设备厂家及行业内资深工程师请教，将晦涩的理论转化为现场可执行的验收标准；最后是'现场验证，知行合一'，纸上得来终觉浅，我会深入施工一线，全程跟踪首件施工，通过亲手实测实量、旁站观察来验证理论知识，并详细记录施工参数与质量表现。同时，我会发挥自身数字化特长，将学习成果迅速转化为结构化的验收检查表或移动端核查工具。通过这种'理论+专家+现场+工具'的四位一体学习法，我能迅速将知识盲区转化为专业壁垒，确保在质监工作中始终掌握技术话语权。"
    },
    {
      "question": "在过往的网络安全与运维工作中，你非常强调'数据资产'与'合规性'。当前基建领域同样面临工程档案造假、验收数据不实等顽疾。如果由你负责基建工程数据质量管理，你将如何运用信息化手段与管理制度，彻底根治'假数据'问题，确保工程全生命周期数据的真实可追溯？",
      "skills": ["数据治理", "合规管控"],
      "description": "基于数据治理专长，考察运用技术手段与制度约束保障基建数据真实性、完整性的能力",
      "answer": "基建数据造假往往源于人为干预空间大与追溯成本高。结合我过往在网络安全中'防篡改、强审计'的经验，我将从技术与制度双管齐下根治这一顽疾。在技术层面，我将推动基建质监数据的'源头直采'与'上链存证'，例如推广使用带有防作弊功能的智能检测仪器，将实测数据通过物联网直接上传至管控平台，减少人工录入环节；同时利用区块链或时间戳技术对关键验收节点的数据进行固化，确保数据一旦生成便不可篡改、全程留痕。在制度层面，我将建立'数据质量终身责任制'与'异常数据熔断机制'，明确施工单位、监理单位对数据真实性的主体责任，一旦发现数据造假不仅严厉处罚当事人，还要扣减企业信用分；此外，我会利用大数据分析建立数据逻辑校验模型，对明显违背工程规律的异常数据进行自动预警与拦截。通过让数据'不能假、不敢假、不想假'，为电网基建工程打造一本真实可信的'数字身份证'。"
    },
    {
      "question": "技术质量管理专责不仅需要过硬的技术，还需要极强的文字综合能力与政策敏感度。你在电科院跟班学习期间负责过国家级项目的策划与资料打磨，请谈谈你是如何将这种高规格的'科研严谨性'转化为日常基建质监工作中的'标准执行力'与'公文规范性'的？",
      "skills": ["文字综合", "标准执行"],
      "description": "基于科研经历与日常工作的反差，考察文字功底、政策理解力及将高标准落实到基层的执行力",
      "answer": "在电科院跟班学习期间，我深刻体会到国家级项目对逻辑严密性、数据准确性和表述规范性的极致要求，这种'科研严谨性'正是日常基建质监工作所急需的。首先，在标准执行上，我将科研中'字斟句酌、交叉验证'的习惯带入质监工作，在编制质量监督大纲、下发整改通知书或撰写工程验收报告时，严格对标国家强制性条文与公司管理制度，确保每一个定性判断都有据可查、每一项整改要求都具备可操作性，杜绝模棱两可的表述。其次，在公文规范与政策转化上，我擅长将上级宏观的政策文件拆解为基层易懂、易执行的标准化作业指导书。例如，我会将冗长的质量管理规定提炼为图文并茂的'口袋书'或'一图读懂'，降低一线人员的理解门槛。最后，在总结复盘方面，我坚持用科研报告的逻辑来撰写质量分析报告，不仅罗列问题，更深挖背后的管理漏洞与技术成因，并提出系统性的改进建议。这种从'宏观政策'到'微观执行'的无缝转化能力，将有效提升基建质监工作的规范化与权威性。"
    }
  ]
}}

# 现在，请根据以下岗位信息、简历数据、候选人优劣势，生成唯一一个JSON输出，不要有任何额外解释。

## 岗位信息：
- 通用岗位：{job_title}
- 岗位描述：{job_description}

{resume}
## 候选人优势：
{strengths}

## 候选人劣势：
{weaknesses}
"""


@dataclass
class RequestResult:
    concurrency: int
    request_id: int
    burst_id: int
    success: bool
    status_code: Optional[int]
    error: str
    start_epoch: float
    end_epoch: float
    send_perf: float
    total_ms: float
    header_ms: Optional[float] = None
    first_byte_ms: Optional[float] = None
    first_token_ms: Optional[float] = None
    response_bytes: int = 0
    stream_events: int = 0
    output_chars: int = 0
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None


@dataclass
class StepResult:
    concurrency: int
    burst_rounds: int
    attempted_requests: int
    completed_requests: int
    success_count: int
    failed_count: int
    success_rate: float
    total_duration_s: float
    effective_duration_s: float
    success_qps: float
    total_qps: float
    configured_concurrency: int
    observed_peak_inflight: int
    full_concurrency_bursts: int
    avg_response_ms: Optional[float]
    p50_response_ms: Optional[float]
    p90_response_ms: Optional[float]
    p95_response_ms: Optional[float]
    p99_response_ms: Optional[float]
    min_response_ms: Optional[float]
    max_response_ms: Optional[float]
    avg_all_request_ms: Optional[float]
    p95_all_request_ms: Optional[float]
    p99_all_request_ms: Optional[float]
    avg_ttfb_ms: Optional[float]
    p95_ttfb_ms: Optional[float]
    avg_ttft_ms: Optional[float]
    p95_ttft_ms: Optional[float]
    total_completion_tokens: int
    token_usage_coverage: float
    output_token_throughput: Optional[float]
    error_summary: dict[str, int] = field(default_factory=dict)


@dataclass
class ModeRunResult:
    stream: bool
    steps: list[StepResult]
    breaking: Optional[tuple[int, str]]
    report_files: dict[str, Path] = field(default_factory=dict)


def make_headers(app_key: str, secret_key: str) -> dict[str, str]:
    x_date = formatdate(timeval=time.time(), localtime=False, usegmt=True)
    sign_text = f"x-date: {x_date}"
    signature = base64.b64encode(
        hmac.new(secret_key.encode("utf-8"), sign_text.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")
    return {
        "x-date": x_date,
        "authorization": (
            f'hmac username="{app_key}", algorithm="hmac-sha256", '
            f'headers="x-date", signature="{signature}"'
        ),
        "Content-Type": "application/json",
    }


def read_text_arg(value: str) -> str:
    if value.startswith("@"):
        return Path(value[1:]).read_text(encoding="utf-8")
    return value


def image_to_content_item(path: str) -> dict[str, Any]:
    image_path = Path(path)
    data = base64.b64encode(image_path.read_bytes()).decode("ascii")
    mime_type = mimetypes.guess_type(image_path.name)[0] or "image/jpeg"
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime_type};base64,{data}"},
    }


def make_payload(args: argparse.Namespace) -> dict[str, Any]:
    user_prompt = read_text_arg(args.user_prompt)
    system_prompt = read_text_arg(args.system_prompt)
    user_content: Any = user_prompt

    if args.image:
        user_content = [{"type": "text", "text": user_prompt}]
        user_content.extend(image_to_content_item(path) for path in args.image)

    payload: dict[str, Any] = {
        "componentCode": args.component_code,
        "model": args.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "stream": args.stream,
    }
    if args.max_tokens is not None:
        payload["max_tokens"] = args.max_tokens
    if args.temperature is not None:
        payload["temperature"] = args.temperature
    return payload


def parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def pick_text(data: Any) -> str:
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        return "".join(pick_text(item) for item in data)
    if not isinstance(data, dict):
        return ""

    choices = data.get("choices")
    if isinstance(choices, list):
        parts: list[str] = []
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            for key in ("delta", "message"):
                nested = choice.get(key)
                if isinstance(nested, dict):
                    content = nested.get("content")
                    if isinstance(content, str):
                        parts.append(content)
            if isinstance(choice.get("text"), str):
                parts.append(choice["text"])
        if parts:
            return "".join(parts)

    for key in ("content", "text", "answer", "result", "output", "response", "data"):
        text = pick_text(data.get(key))
        if text:
            return text
    return ""


def find_usage(data: Any) -> Optional[dict[str, Any]]:
    if isinstance(data, dict):
        usage = data.get("usage")
        if isinstance(usage, dict):
            return usage
        for value in data.values():
            nested = find_usage(value)
            if nested is not None:
                return nested
    elif isinstance(data, list):
        for item in data:
            nested = find_usage(item)
            if nested is not None:
                return nested
    return None


def to_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def extract_usage(data: Any) -> tuple[Optional[int], Optional[int], Optional[int]]:
    usage = find_usage(data) or {}
    prompt_tokens = first_int(usage, ("prompt_tokens", "input_tokens", "prompt_token_count"))
    completion_tokens = first_int(
        usage,
        ("completion_tokens", "output_tokens", "generated_tokens", "completion_token_count"),
    )
    total_tokens = first_int(usage, ("total_tokens", "total_token_count"))
    if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
        total_tokens = prompt_tokens + completion_tokens
    if completion_tokens is None and prompt_tokens is not None and total_tokens is not None:
        completion_tokens = max(total_tokens - prompt_tokens, 0)
    return prompt_tokens, completion_tokens, total_tokens


def first_int(data: dict[str, Any], keys: tuple[str, ...]) -> Optional[int]:
    for key in keys:
        value = to_int(data.get(key))
        if value is not None:
            return value
    return None


def validate_body(data: Any) -> tuple[bool, str]:
    nested_error = find_business_error(data)
    if nested_error:
        return False, nested_error
    return True, ""


def find_business_error(data: Any) -> str:
    if not isinstance(data, dict):
        if isinstance(data, list):
            for item in data:
                nested = find_business_error(item)
                if nested:
                    return nested
        return ""

    if data.get("error"):
        return f"Business error: {str(data.get('error'))[:200]}"

    success = data.get("success")
    if isinstance(success, bool) and not success:
        return f"Business success=false: {str(data)[:200]}"

    for key in ("code", "status_code", "error_code"):
        if key in data:
            value = data.get(key)
            numeric = to_int(value)
            if numeric is not None and numeric not in (0, 200):
                return f"Business {key}: {value}"
            if numeric is None:
                text = str(value).strip().lower()
                if text and text not in {"0", "200", "ok", "success", "succeeded"}:
                    return f"Business {key}: {value}"

    status = str(data.get("status", "")).strip().lower()
    if status in {"error", "failed", "failure", "fail"}:
        return f"Business status: {data.get('status')}"

    for value in data.values():
        nested = find_business_error(value)
        if nested:
            return nested
    return ""


def percentile(values: list[float], pct: float) -> Optional[float]:
    if not values:
        return None
    if pct < 0 or pct > 100:
        raise ValueError("percentile must be in [0, 100]")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (pct / 100)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    fraction = rank - low
    return ordered[low] + (ordered[high] - ordered[low]) * fraction


def average(values: list[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def format_ms(value: Optional[float]) -> str:
    return "N/A" if value is None else f"{value:.2f}ms"


def format_number(value: Optional[float], suffix: str = "", digits: int = 2) -> str:
    return "N/A" if value is None else f"{value:.{digits}f}{suffix}"


def is_timeout_error(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return True
    return "timed out" in str(exc).lower() or "timeout" in str(reason).lower()


class InflightCounter:
    def __init__(self) -> None:
        self.current = 0
        self.peak = 0
        self.lock = threading.Lock()

    def enter(self) -> None:
        with self.lock:
            self.current += 1
            self.peak = max(self.peak, self.current)

    def leave(self) -> None:
        with self.lock:
            self.current -= 1


class PeakTracker:
    def __init__(self) -> None:
        self.peak = 0

    def observe(self, value: int) -> None:
        self.peak = max(self.peak, value)


class StartGate:
    def __init__(self, target_ready: int) -> None:
        self.target_ready = target_ready
        self.ready = 0
        self.condition = threading.Condition()
        self.event = threading.Event()

    def ready_and_wait(self) -> None:
        with self.condition:
            self.ready += 1
            self.condition.notify_all()
        self.event.wait()

    def wait_until_ready(self, timeout: float = 30.0) -> bool:
        deadline = time.perf_counter() + timeout
        with self.condition:
            while self.ready < self.target_ready:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    return False
                self.condition.wait(remaining)
            return True

    def release(self) -> None:
        self.event.set()


class GatewayConcurrentTester:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.payload = make_payload(args)
        self.payload_bytes = json.dumps(self.payload, ensure_ascii=False).encode("utf-8")
        self.ssl_context = None if args.verify_ssl else ssl._create_unverified_context()

    def send_request(
        self,
        request_id: int,
        concurrency: int,
        burst_id: int,
        start_gate: StartGate,
        inflight: InflightCounter,
    ) -> RequestResult:
        start_epoch = 0.0
        start_perf = 0.0
        entered_inflight = False
        status_code: Optional[int] = None
        header_ms: Optional[float] = None
        first_byte_ms: Optional[float] = None
        first_token_ms: Optional[float] = None
        response_bytes = 0
        stream_events = 0
        output_parts: list[str] = []
        usage_source: Any = None

        try:
            request = urllib.request.Request(
                self.args.url,
                data=self.payload_bytes,
                headers=make_headers(self.args.app_key, self.args.secret_key),
                method="POST",
            )
            start_gate.ready_and_wait()

            inflight.enter()
            entered_inflight = True
            start_epoch = time.time()
            start_perf = time.perf_counter()

            try:
                response = urllib.request.urlopen(
                    request,
                    timeout=self.args.timeout,
                    context=self.ssl_context,
                )
            except urllib.error.HTTPError as exc:
                status_code = exc.code
                try:
                    error_body = exc.read(4096).decode("utf-8", errors="replace")
                    parsed_error = parse_json(error_body)
                    ok, business_error = validate_body(parsed_error)
                    error_message = business_error if not ok else f"HTTP {exc.code}: {error_body[:200]}"
                    return self._failure(
                        concurrency,
                        request_id,
                        start_epoch,
                        start_perf,
                        error_message,
                        status_code,
                        burst_id,
                    )
                finally:
                    exc.close()

            with response:
                status_code = response.getcode()
                header_ms = (time.perf_counter() - start_perf) * 1000
                if self.args.stream:
                    stream_result = self._read_stream(response, start_perf)
                    (
                        response_bytes,
                        stream_events,
                        first_byte_ms,
                        first_token_ms,
                        output_parts,
                        usage_source,
                    ) = stream_result
                else:
                    raw_body, response_bytes, first_byte_ms = self._read_body(response, start_perf)
                    text = raw_body.decode("utf-8", errors="replace")
                    usage_source = parse_json(text)
                    output_parts.append(pick_text(usage_source) or text)

            end_perf = time.perf_counter()
            end_epoch = time.time()
            data_for_validation = usage_source
            ok, error = validate_body(data_for_validation)
            if not ok:
                return self._failure(
                    concurrency,
                    request_id,
                    start_epoch,
                    start_perf,
                    error,
                    status_code,
                    burst_id,
                )

            prompt_tokens, completion_tokens, total_tokens = extract_usage(usage_source)
            output_text = "".join(output_parts).strip()
            success = 200 <= (status_code or 0) < 300
            error = "" if success else f"HTTP {status_code}"
            if success and not output_text:
                success = False
                error = "Empty output"
            if success and self.args.stream and stream_events <= 0:
                success = False
                error = "No stream events"

            return RequestResult(
                concurrency=concurrency,
                request_id=request_id,
                burst_id=burst_id,
                success=success,
                status_code=status_code,
                error=error,
                start_epoch=start_epoch,
                end_epoch=end_epoch,
                send_perf=start_perf,
                total_ms=(end_perf - start_perf) * 1000,
                header_ms=header_ms,
                first_byte_ms=first_byte_ms,
                first_token_ms=first_token_ms,
                response_bytes=response_bytes,
                stream_events=stream_events,
                output_chars=len(output_text),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            )
        except (TimeoutError, socket.timeout) as exc:
            return self._failure(concurrency, request_id, start_epoch, start_perf, f"Timeout: {exc}", status_code, burst_id)
        except urllib.error.URLError as exc:
            if is_timeout_error(exc):
                return self._failure(concurrency, request_id, start_epoch, start_perf, f"Timeout: {exc}", status_code, burst_id)
            return self._failure(concurrency, request_id, start_epoch, start_perf, f"Connection error: {exc}", status_code, burst_id)
        except Exception as exc:
            return self._failure(concurrency, request_id, start_epoch, start_perf, f"Exception: {exc}", status_code, burst_id)
        finally:
            if entered_inflight:
                inflight.leave()

    def _read_body(self, response: Any, start_perf: float) -> tuple[bytes, int, Optional[float]]:
        chunks: list[bytes] = []
        response_bytes = 0
        first_byte_ms = None
        first = response.read(1)
        if first:
            first_byte_ms = (time.perf_counter() - start_perf) * 1000
            response_bytes += len(first)
            chunks.append(first)
        while True:
            chunk = response.read(8192)
            if not chunk:
                break
            if first_byte_ms is None:
                first_byte_ms = (time.perf_counter() - start_perf) * 1000
            response_bytes += len(chunk)
            chunks.append(chunk)
        return b"".join(chunks), response_bytes, first_byte_ms

    def _read_stream(
        self,
        response: Any,
        start_perf: float,
    ) -> tuple[int, int, Optional[float], Optional[float], list[str], Any]:
        response_bytes = 0
        stream_events = 0
        first_byte_ms = None
        first_token_ms = None
        output_parts: list[str] = []
        last_json: Any = None
        usage_source: Any = None

        # SSE event data may span multiple `data:` lines; process only complete events.
        event_data_lines: list[str] = []
        first_byte = response.read(1)
        if first_byte:
            first_byte_ms = (time.perf_counter() - start_perf) * 1000
            pending_line = first_byte + response.readline()
        else:
            pending_line = b""

        while raw_line := (pending_line or response.readline()):
            pending_line = b""
            response_bytes += len(raw_line)
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                if not event_data_lines:
                    continue
                event_data = "\n".join(event_data_lines)
                event_data_lines.clear()
                if event_data == "[DONE]":
                    break
                stream_events += 1
                event = parse_json(event_data)
                ok, error = validate_body(event)
                if not ok:
                    raise RuntimeError(error)
                last_json = event
                if find_usage(event) is not None:
                    usage_source = event
                text = pick_text(event)
                if text:
                    output_parts.append(text)
                    if first_token_ms is None:
                        first_token_ms = (time.perf_counter() - start_perf) * 1000
                continue
            if line.startswith("data:"):
                event_data_lines.append(line[5:].lstrip())
            elif not line.startswith(":") and not event_data_lines:
                # Support gateways that return newline-delimited JSON rather than SSE.
                event_data_lines.append(line)

        if event_data_lines:
            event_data = "\n".join(event_data_lines)
            if event_data != "[DONE]":
                stream_events += 1
                event = parse_json(event_data)
                ok, error = validate_body(event)
                if not ok:
                    raise RuntimeError(error)
                last_json = event
                if find_usage(event) is not None:
                    usage_source = event
                text = pick_text(event)
                if text:
                    output_parts.append(text)
                    if first_token_ms is None:
                        first_token_ms = (time.perf_counter() - start_perf) * 1000

        return response_bytes, stream_events, first_byte_ms, first_token_ms, output_parts, usage_source or last_json

    @staticmethod
    def _failure(
        concurrency: int,
        request_id: int,
        start_epoch: float,
        start_perf: float,
        error: str,
        status_code: Optional[int],
        burst_id: int,
    ) -> RequestResult:
        end_perf = time.perf_counter()
        started = start_perf > 0
        return RequestResult(
            concurrency=concurrency,
            request_id=request_id,
            burst_id=burst_id,
            success=False,
            status_code=status_code,
            error=error[:300],
            start_epoch=start_epoch if started else time.time(),
            end_epoch=time.time(),
            send_perf=start_perf if started else 0.0,
            total_ms=(end_perf - start_perf) * 1000 if started else 0.0,
        )

    def run_step(self, concurrency: int, total_requests: int) -> tuple[StepResult, list[RequestResult]]:
        results: list[RequestResult] = []
        completed = 0
        progress_every = max(1, total_requests // 10)
        burst_count = (total_requests + concurrency - 1) // concurrency
        peak_tracker = PeakTracker()

        print(f"\n{'=' * 72}")
        print(
            f"开始测试并发 {concurrency}: 请求数={total_requests}, "
            f"同步批次={burst_count}, stream={self.args.stream}"
        )
        print(f"{'=' * 72}")
        start = time.perf_counter()
        next_request_id = 1
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
            for burst_id in range(1, burst_count + 1):
                burst_size = min(concurrency, total_requests - completed)
                start_gate = StartGate(burst_size)
                inflight = InflightCounter()
                futures = [
                    executor.submit(
                        self.send_request,
                        request_id,
                        concurrency,
                        burst_id,
                        start_gate,
                        inflight,
                    )
                    for request_id in range(next_request_id, next_request_id + burst_size)
                ]
                next_request_id += burst_size

                if not start_gate.wait_until_ready(timeout=self.args.start_timeout):
                    start_gate.release()
                    raise RuntimeError(
                        f"并发 {concurrency} 第 {burst_id} 批启动超时: "
                        f"仅 {start_gate.ready}/{burst_size} 个 worker 就绪"
                    )

                burst_start = time.perf_counter()
                print(f"第 {burst_id}/{burst_count} 批释放: {burst_size} 个请求同时发起")
                start_gate.release()

                for future in concurrent.futures.as_completed(futures):
                    result = future.result()
                    results.append(result)
                    completed += 1
                    if completed % progress_every == 0 or completed == total_requests:
                        ok_count = sum(1 for item in results if item.success)
                        print(f"进度: {completed}/{total_requests}, 当前成功率={ok_count / completed * 100:.2f}%")

                peak_tracker.observe(inflight.peak)
                if burst_id != burst_count and self.args.burst_interval > 0:
                    elapsed = time.perf_counter() - burst_start
                    sleep_seconds = max(self.args.burst_interval - elapsed, 0.0)
                    if sleep_seconds > 0:
                        time.sleep(sleep_seconds)

        total_duration = time.perf_counter() - start
        step = summarize_step(concurrency, total_requests, total_duration, peak_tracker.peak, burst_count, results)
        print_step_report(step)
        return step, sorted(results, key=lambda item: item.request_id)


def summarize_step(
    concurrency: int,
    total_requests: int,
    total_duration: float,
    observed_peak: int,
    burst_rounds: int,
    results: list[RequestResult],
) -> StepResult:
    success = [item for item in results if item.success]
    failed = [item for item in results if not item.success]
    sent_results = [item for item in results if item.send_perf > 0]
    # Exclude configured idle time between bursts from the QPS measurement window.
    burst_durations: list[float] = []
    for burst_id in {item.burst_id for item in sent_results}:
        burst_results = [item for item in sent_results if item.burst_id == burst_id]
        burst_start = min(item.send_perf for item in burst_results)
        burst_end = max(item.send_perf + item.total_ms / 1000 for item in burst_results)
        burst_durations.append(max(burst_end - burst_start, 0.0))
    effective_duration = sum(burst_durations)
    response_times = [item.total_ms for item in success]
    all_request_times = [item.total_ms for item in sent_results]
    ttfb_times = [item.first_byte_ms for item in success if item.first_byte_ms is not None]
    ttft_times = [item.first_token_ms for item in success if item.first_token_ms is not None]
    completion_tokens = [
        item.completion_tokens for item in success if item.completion_tokens is not None
    ]
    total_completion_tokens = sum(completion_tokens)
    token_coverage = (len(completion_tokens) / len(success) * 100) if success else 0.0
    usage_complete = bool(success) and len(completion_tokens) == len(success)
    error_summary = Counter(normalize_error(item.error) for item in failed)

    return StepResult(
        concurrency=concurrency,
        burst_rounds=burst_rounds,
        attempted_requests=total_requests,
        completed_requests=len(results),
        success_count=len(success),
        failed_count=len(failed),
        success_rate=(len(success) / total_requests * 100) if total_requests else 0.0,
        total_duration_s=total_duration,
        effective_duration_s=effective_duration,
        success_qps=(len(success) / effective_duration) if effective_duration > 0 else 0.0,
        total_qps=(len(sent_results) / effective_duration) if effective_duration > 0 else 0.0,
        configured_concurrency=concurrency,
        observed_peak_inflight=observed_peak,
        full_concurrency_bursts=total_requests // concurrency,
        avg_response_ms=average(response_times),
        p50_response_ms=percentile(response_times, 50),
        p90_response_ms=percentile(response_times, 90),
        p95_response_ms=percentile(response_times, 95),
        p99_response_ms=percentile(response_times, 99),
        min_response_ms=min(response_times) if response_times else None,
        max_response_ms=max(response_times) if response_times else None,
        avg_all_request_ms=average(all_request_times),
        p95_all_request_ms=percentile(all_request_times, 95),
        p99_all_request_ms=percentile(all_request_times, 99),
        avg_ttfb_ms=average(ttfb_times),
        p95_ttfb_ms=percentile(ttfb_times, 95),
        avg_ttft_ms=average(ttft_times),
        p95_ttft_ms=percentile(ttft_times, 95),
        total_completion_tokens=total_completion_tokens,
        token_usage_coverage=token_coverage,
        output_token_throughput=(
            total_completion_tokens / effective_duration
            if effective_duration > 0 and usage_complete
            else None
        ),
        error_summary=dict(error_summary),
    )


def normalize_error(error: str) -> str:
    if not error:
        return "Unknown"
    if ":" in error:
        return error.split(":", 1)[0]
    return error[:80]


def print_step_report(step: StepResult) -> None:
    print(f"\n并发 {step.concurrency} 测试结果")
    print(f"计划请求数: {step.attempted_requests}")
    print(f"完成请求数: {step.completed_requests}")
    print(f"成功/失败: {step.success_count}/{step.failed_count} ({step.success_rate:.2f}%)")
    print(
        f"同步批次: {step.burst_rounds}, "
        f"满并发批次: {step.full_concurrency_bursts}/{step.burst_rounds}"
    )
    print(f"目标并发/实际峰值并发: {step.configured_concurrency}/{step.observed_peak_inflight}")
    print(f"总耗时: {step.total_duration_s:.2f}s")
    print(f"有效压测耗时: {step.effective_duration_s:.2f}s")
    print(f"总QPS/成功QPS: {step.total_qps:.2f}/{step.success_qps:.2f} (基于有效压测耗时)")
    print(
        "成功请求响应耗时: "
        f"avg={format_ms(step.avg_response_ms)}, "
        f"p50={format_ms(step.p50_response_ms)}, "
        f"p95={format_ms(step.p95_response_ms)}, "
        f"p99={format_ms(step.p99_response_ms)}, "
        f"max={format_ms(step.max_response_ms)}"
    )
    print(
        "全部已发送请求端到端耗时: "
        f"avg={format_ms(step.avg_all_request_ms)}, "
        f"p95={format_ms(step.p95_all_request_ms)}, "
        f"p99={format_ms(step.p99_all_request_ms)}"
    )
    print(f"TTFB: avg={format_ms(step.avg_ttfb_ms)}, p95={format_ms(step.p95_ttfb_ms)}")
    print(f"TTFT(stream文本首包): avg={format_ms(step.avg_ttft_ms)}, p95={format_ms(step.p95_ttft_ms)}")
    print(
        "输出 token 吞吐: "
        f"{format_number(step.output_token_throughput, ' tok/s')} "
        f"(仅 usage 完整覆盖时计算，usage覆盖率={step.token_usage_coverage:.2f}%)"
    )
    if step.error_summary:
        print("失败原因统计:")
        for error, count in sorted(step.error_summary.items(), key=lambda item: item[1], reverse=True):
            print(f"  {error}: {count}")


def is_breaking_point(
    current: StepResult,
    previous: Optional[StepResult],
    success_threshold: float,
    latency_growth_threshold: float,
) -> tuple[bool, str]:
    if current.success_rate < success_threshold:
        return True, f"成功率 {current.success_rate:.2f}% < 阈值 {success_threshold:.2f}%"
    if (
        previous
        and previous.p95_response_ms
        and current.p95_response_ms
        and current.p95_response_ms > previous.p95_response_ms * latency_growth_threshold
    ):
        return (
            True,
            f"P95 响应耗时从 {previous.p95_response_ms:.2f}ms 增长到 "
            f"{current.p95_response_ms:.2f}ms，超过 {latency_growth_threshold:.2f} 倍",
        )
    return False, ""


def build_final_report(
    args: argparse.Namespace,
    steps: list[StepResult],
    breaking: Optional[tuple[int, str]],
    report_files: dict[str, Path],
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if args.concurrent:
        level_text = f"指定并发: {args.concurrent}"
        effective_break_confirmations = 1
    else:
        level_text = ", ".join(str(step.concurrency) for step in steps)
        effective_break_confirmations = 1 if len(steps) == 1 else args.break_confirmations
    stable_steps = [
        step
        for step in steps
        if step.success_rate >= args.success_threshold
        and (breaking is None or step.concurrency < breaking[0])
    ]
    best_stable_qps = max(stable_steps, key=lambda item: item.success_qps) if stable_steps else None
    best_observed_qps = max(steps, key=lambda item: item.success_qps) if steps else None
    last_stable = stable_steps[-1] if stable_steps else None

    if breaking:
        limit_text = (
            f"首次拐点/极限风险出现在并发 {breaking[0]}：{breaking[1]}。"
            f"建议把稳定并发上限暂定为 {last_stable.concurrency if last_stable else 'N/A'}。"
        )
    else:
        limit_text = (
            f"本次范围内未触发拐点，稳定并发上限至少达到 {steps[-1].concurrency if steps else 'N/A'}。"
        )

    lines = [
        "# Qwen3 阶梯并发测试报告",
        "",
        f"- 生成时间: {now}",
        f"- URL: {args.url}",
        f"- 模型: {args.model}",
        f"- componentCode: {args.component_code}",
        f"- stream: {args.stream}",
        f"- 模式执行方式: {mode_execution_name(args)}",
        f"- 请求计划: {format_request_plan(args)}",
        f"- 并发级别: {level_text}",
        f"- 成功率阈值: {args.success_threshold:.2f}%",
        f"- P95增长拐点阈值: {args.latency_growth_threshold:.2f}倍",
        "",
        "## 结论",
        "",
        f"- {limit_text}",
    ]
    if best_stable_qps:
        lines.append(
            f"- 最佳稳定成功QPS: 并发 {best_stable_qps.concurrency}，"
            f"成功QPS={best_stable_qps.success_qps:.2f}，"
            f"成功率={best_stable_qps.success_rate:.2f}%，P95={format_ms(best_stable_qps.p95_response_ms)}。"
        )
    elif best_observed_qps:
        lines.append(
            f"- 本次没有成功率达到阈值的阶梯；仅供观察的最高成功QPS出现在并发 "
            f"{best_observed_qps.concurrency}，成功QPS={best_observed_qps.success_qps:.2f}，"
            f"成功率={best_observed_qps.success_rate:.2f}%。"
        )
    lines.extend(
        [
            "- 口径说明: 每个同步批次会先创建好请求对象并等待全部 worker 就绪，再统一释放发起网络请求；"
            "响应耗时从释放后开始统计，包含网络传输、服务端处理和响应读取。",
            "- 总耗时包含同一阶梯内各 burst 之间的等待间隔；有效压测耗时为各 burst 从首个请求发出到最后一个请求完成的时长之和，"
            "不含 burst 之间的空闲等待，QPS 和 token 吞吐均基于该时长计算。",
            "- TTFB 为响应体首字节耗时；TTFT 仅在 stream=True 且首次出现有效文本增量时统计；"
            "token 吞吐仅在所有成功请求均返回 completion token usage 时统计；否则显示 N/A，避免部分 usage 导致失真。",
            f"- 拐点确认: 本次按连续 {effective_break_confirmations} 个阶梯触发风险条件确认拐点；"
            "若成功率低于提前停止阈值，则直接确认当前风险点。",
            "",
            "## 阶梯结果",
            "",
            "| 并发 | 同步批次 | 满并发批次 | 实际峰值 | 请求数 | 成功率 | 总耗时 | 有效压测耗时 | 总QPS | 成功QPS | 成功P95响应 | 全请求P95端到端 | 成功P99响应 | 全请求P99端到端 | P95 TTFB | P95 TTFT | token吞吐 | usage覆盖率 |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for step in steps:
        lines.append(
            f"| {step.concurrency} | {step.burst_rounds} | "
            f"{step.full_concurrency_bursts} | {step.observed_peak_inflight} | {step.attempted_requests} | "
            f"{step.success_rate:.2f}% | {step.total_duration_s:.2f}s | {step.effective_duration_s:.2f}s | "
            f"{step.total_qps:.2f} | {step.success_qps:.2f} | {format_ms(step.p95_response_ms)} | "
            f"{format_ms(step.p95_all_request_ms)} | {format_ms(step.p99_response_ms)} | "
            f"{format_ms(step.p99_all_request_ms)} | {format_ms(step.p95_ttfb_ms)} | "
            f"{format_ms(step.p95_ttft_ms)} | {format_number(step.output_token_throughput, ' tok/s')} | "
            f"{step.token_usage_coverage:.2f}% |"
        )

    lines.extend(["", "## 输出文件", ""])
    for name, path in report_files.items():
        lines.append(f"- {name}: {path}")
    return "\n".join(lines)


def format_request_plan(args: argparse.Namespace) -> str:
    if args.total is None:
        return f"每档按 并发数 x {args.rounds} 轮 自动计算"
    return "每档至少 {0} 个请求；不足满批次时自动补齐到当前并发整数倍".format(args.total)


def stream_mode_name(stream: bool) -> str:
    return "流式" if stream else "非流式"


def stream_mode_file_label(stream: bool) -> str:
    return "stream" if stream else "non_stream"


def mode_execution_name(args: argparse.Namespace) -> str:
    if args.parallel_stream_modes:
        return "流式与非流式同时压测（每种模式各使用配置的并发）"
    if args.both_stream_modes:
        return "流式与非流式依次压测"
    return "单模式压测"


def write_reports(
    args: argparse.Namespace,
    steps: list[StepResult],
    details: list[RequestResult],
    breaking: Optional[tuple[int, str]],
    report_label: str = "",
) -> dict[str, Path]:
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    label_suffix = f"_{report_label}" if report_label else ""
    prefix = f"qwen3_vl_concurrent{label_suffix}_{timestamp}"
    summary_csv = report_dir / f"{prefix}_summary.csv"
    detail_csv = report_dir / f"{prefix}_details.csv"
    markdown = report_dir / f"{prefix}_report.md"

    with summary_csv.open("w", newline="", encoding="utf-8-sig") as file:
        fieldnames = list(StepResult.__dataclass_fields__.keys())
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for step in steps:
            row = step.__dict__.copy()
            row["error_summary"] = json.dumps(row["error_summary"], ensure_ascii=False)
            writer.writerow(row)

    with detail_csv.open("w", newline="", encoding="utf-8-sig") as file:
        fieldnames = list(RequestResult.__dataclass_fields__.keys())
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for item in details:
            writer.writerow(item.__dict__)

    files = {"Markdown报告": markdown, "汇总CSV": summary_csv, "明细CSV": detail_csv}
    report_text = build_final_report(args, steps, breaking, files)
    markdown.write_text(report_text + "\n", encoding="utf-8")
    return files


def build_comparison_report(args: argparse.Namespace, mode_runs: list[ModeRunResult]) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# Qwen3 流式与非流式对比报告",
        "",
        f"- 生成时间: {now}",
        f"- URL: {args.url}",
        f"- 模型: {args.model}",
        f"- componentCode: {args.component_code}",
        f"- 请求计划: {format_request_plan(args)}",
        f"- 成功率阈值: {args.success_threshold:.2f}%",
        f"- 模式执行方式: {mode_execution_name(args)}",
        "- 注意: 同时压测时两种模式会共享网关和模型资源；表中每种模式的并发均为单独配置值，总入口并发约为两者之和。",
        "",
        "## 对比摘要",
        "",
        "| 模式 | 已测并发 | 首次拐点/风险 | 建议稳定并发上限 | 最佳达标成功QPS | 对应P95响应 |",
        "|---|---|---|---:|---:|---:|",
    ]
    for mode_run in mode_runs:
        stable_steps = [
            step
            for step in mode_run.steps
            if step.success_rate >= args.success_threshold
            and (mode_run.breaking is None or step.concurrency < mode_run.breaking[0])
        ]
        best_stable_qps = max(stable_steps, key=lambda item: item.success_qps) if stable_steps else None
        levels = ", ".join(str(step.concurrency) for step in mode_run.steps) or "N/A"
        breaking = (
            f"并发 {mode_run.breaking[0]}: {mode_run.breaking[1]}"
            if mode_run.breaking
            else "本次范围内未触发"
        )
        stable_limit = stable_steps[-1].concurrency if stable_steps else "N/A"
        best_qps = f"{best_stable_qps.success_qps:.2f}" if best_stable_qps else "N/A"
        p95 = format_ms(best_stable_qps.p95_response_ms) if best_stable_qps else "N/A"
        lines.append(
            f"| {stream_mode_name(mode_run.stream)} | {levels} | {breaking} | "
            f"{stable_limit} | {best_qps} | {p95} |"
        )

    lines.extend(["", "## 模式报告", ""])
    for mode_run in mode_runs:
        lines.append(f"### {stream_mode_name(mode_run.stream)}")
        for name, path in mode_run.report_files.items():
            lines.append(f"- {name}: {path}")
        lines.append("")
    return "\n".join(lines)


def write_comparison_report(args: argparse.Namespace, mode_runs: list[ModeRunResult]) -> Path:
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    comparison_report = report_dir / f"qwen3_vl_concurrent_stream_comparison_{timestamp}.md"
    comparison_report.write_text(build_comparison_report(args, mode_runs) + "\n", encoding="utf-8")
    return comparison_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Qwen3 阶梯并发压测脚本（仅使用 Python 标准库）")
    parser.add_argument("--app-key", default=APP_KEY)
    parser.add_argument("--secret-key", default=SECRET_KEY)
    parser.add_argument("--url", default=URL)
    parser.add_argument("--component-code", default=COMPONENT_CODE)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--system-prompt", default=DEFAULT_SYSTEM_PROMPT, help="系统提示词；以 @file.txt 形式读取文件")
    parser.add_argument("--user-prompt", default=DEFAULT_USER_PROMPT, help="用户提示词；以 @file.txt 形式读取文件")
    parser.add_argument("--image", action="append", help="可选图片路径，可重复传入；默认纯文本请求")
    parser.add_argument("--stream", dest="stream", action="store_true", default=True)
    parser.add_argument("--no-stream", dest="stream", action="store_false")
    parser.add_argument(
        "--both-stream-modes",
        "--all-stream-modes",
        dest="both_stream_modes",
        action="store_true",
        help="同一次运行中依次测试流式和非流式；忽略 --stream/--no-stream 的单模式选择",
    )
    parser.add_argument(
        "--parallel-stream-modes",
        action="store_true",
        help="流式和非流式同时压测；每种模式各使用配置的并发，网关总并发约为两者之和",
    )
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--verify-ssl", action="store_true", help="默认不校验证书；传入该参数后启用证书校验")
    parser.add_argument("--concurrent", type=int, default=None, help="只测试一个指定并发；不传则执行阶梯并发")
    parser.add_argument("--total", type=int, default=None, help="每个并发级别的最少请求总数；会自动补齐为当前并发的整数倍")
    parser.add_argument("--rounds", type=int, default=5, help="未指定 --total 时，每个并发级别执行多少轮同步 burst")
    parser.add_argument("--burst-interval", type=float, default=0.0, help="同一阶梯内两轮同步 burst 的最小间隔秒数")
    parser.add_argument("--start-timeout", type=float, default=30.0, help="等待一轮内所有 worker 就绪的超时时间")
    parser.add_argument("--start-concurrent", type=int, default=1)
    parser.add_argument("--max-concurrent", type=int, default=40) #最大并发数
    parser.add_argument("--step", type=int, default=1)
    parser.add_argument("--success-threshold", type=float, default=95.0, help="判定稳定并发的成功率阈值")
    parser.add_argument("--latency-growth-threshold", type=float, default=2.0, help="相邻阶梯 P95 增长倍数达到该值视为拐点")
    parser.add_argument("--stop-success-rate", type=float, default=50.0, help="成功率低于该值时提前停止")
    parser.add_argument("--break-confirmations", type=int, default=2, help="连续触发多少个阶梯后确认拐点；单阶测试自动按 1 处理")
    parser.add_argument("--report-dir", default="reports")
    parser.add_argument("--print-payload", action="store_true")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.both_stream_modes and args.parallel_stream_modes:
        raise ValueError("--both-stream-modes 和 --parallel-stream-modes 不能同时使用")
    if args.concurrent is not None and args.concurrent <= 0:
        raise ValueError("--concurrent 必须大于 0")
    if args.total is not None and args.total <= 0:
        raise ValueError("--total 必须大于 0")
    if args.rounds <= 0:
        raise ValueError("--rounds 必须大于 0")
    if args.burst_interval < 0:
        raise ValueError("--burst-interval 不能小于 0")
    if args.start_timeout <= 0:
        raise ValueError("--start-timeout 必须大于 0")
    if args.start_concurrent <= 0 or args.max_concurrent <= 0 or args.step <= 0:
        raise ValueError("--start-concurrent、--max-concurrent、--step 必须大于 0")
    if args.start_concurrent > args.max_concurrent:
        raise ValueError("--start-concurrent 不能大于 --max-concurrent")
    if args.timeout <= 0:
        raise ValueError("--timeout 必须大于 0")
    if args.max_tokens is not None and args.max_tokens <= 0:
        raise ValueError("--max-tokens 必须大于 0")
    if not 0 <= args.success_threshold <= 100:
        raise ValueError("--success-threshold 必须在 0 到 100 之间")
    if not 0 <= args.stop_success_rate <= 100:
        raise ValueError("--stop-success-rate 必须在 0 到 100 之间")
    if args.latency_growth_threshold <= 1:
        raise ValueError("--latency-growth-threshold 必须大于 1")
    if args.break_confirmations <= 0:
        raise ValueError("--break-confirmations 必须大于 0")
    for value in args.image or []:
        if not Path(value).is_file():
            raise ValueError(f"--image 指定的文件不存在: {value}")
    for option_name in ("system_prompt", "user_prompt"):
        value = getattr(args, option_name)
        if isinstance(value, str) and value.startswith("@") and not Path(value[1:]).is_file():
            raise ValueError(f"--{option_name.replace('_', '-')} 指定的文件不存在: {value[1:]}")


def has_custom_concurrency_range(argv: list[str]) -> bool:
    range_options = ("--start-concurrent", "--max-concurrent", "--step")
    return any(
        arg == option or arg.startswith(f"{option}=")
        for arg in argv[1:]
        for option in range_options
    )


def resolve_total_requests(args: argparse.Namespace, concurrency: int) -> int:
    requested = concurrency * args.rounds if args.total is None else args.total
    requested = max(requested, concurrency)
    remainder = requested % concurrency
    if remainder:
        requested += concurrency - remainder
    return requested


def run_mode_test(
    args: argparse.Namespace,
    levels: list[int],
) -> tuple[list[StepResult], list[RequestResult], Optional[tuple[int, str]]]:
    tester = GatewayConcurrentTester(args)
    if args.print_payload:
        print(f"{stream_mode_name(args.stream)}请求 payload:")
        print(json.dumps(tester.payload, ensure_ascii=False, indent=2))
    print(f"\nQwen3 阶梯并发测试（{stream_mode_name(args.stream)}）")
    print(f"目标URL: {args.url}")
    print(f"并发级别: {levels}")
    print(f"请求计划: {format_request_plan(args)}")
    print(f"SSL证书校验: {args.verify_ssl}")

    all_steps: list[StepResult] = []
    all_details: list[RequestResult] = []
    previous: Optional[StepResult] = None
    breaking: Optional[tuple[int, str]] = None
    break_streak = 0
    first_break_candidate: Optional[tuple[int, str]] = None
    required_break_confirmations = 1 if len(levels) == 1 else args.break_confirmations

    for level in levels:
        total_requests = resolve_total_requests(args, level)
        if args.total is not None and total_requests != args.total:
            print(
                f"\n提示: 并发 {level} 的 --total={args.total} 不是并发整数倍，"
                f"已补齐为 {total_requests}，确保每轮都是满并发同步发起。"
            )
        step, details = tester.run_step(level, total_requests)
        all_steps.append(step)
        all_details.extend(details)

        is_break, reason = is_breaking_point(
            step,
            previous,
            args.success_threshold,
            args.latency_growth_threshold,
        )
        if is_break:
            if break_streak == 0:
                first_break_candidate = (level, reason)
            break_streak += 1
            print(
                f"\n拐点风险候选: 并发 {level}, 原因: {reason} "
                f"({break_streak}/{required_break_confirmations})"
            )
            if break_streak >= required_break_confirmations and breaking is None:
                breaking = first_break_candidate
                print(f"确认拐点: 并发 {breaking[0]}, 原因: {breaking[1]}")
        else:
            break_streak = 0
            first_break_candidate = None

        previous = step
        if step.success_rate < args.stop_success_rate:
            if breaking is None and first_break_candidate is not None:
                breaking = first_break_candidate
                print(f"因成功率低于提前停止阈值，确认拐点: 并发 {breaking[0]}, 原因: {breaking[1]}")
            print(f"\n成功率 {step.success_rate:.2f}% 低于提前停止阈值 {args.stop_success_rate:.2f}%，停止后续阶梯。")
            break

        if level != levels[-1] and args.concurrent is None:
            time.sleep(2)

    return all_steps, all_details, breaking


def main() -> int:
    args = parse_args()
    try:
        validate_args(args)
    except ValueError as exc:
        print(f"参数错误: {exc}", file=sys.stderr)
        return 2

    if args.concurrent:
        levels = [args.concurrent]
    elif has_custom_concurrency_range(sys.argv):
        levels = list(range(args.start_concurrent, args.max_concurrent + 1, args.step))
    else:
        levels = DEFAULT_CONCURRENCY_LEVELS

    test_both_modes = args.both_stream_modes or args.parallel_stream_modes
    stream_modes = [True, False] if test_both_modes else [args.stream]
    mode_runs: list[ModeRunResult] = []

    def run_and_report(stream: bool) -> ModeRunResult:
        mode_args_dict = vars(args).copy()
        mode_args_dict["stream"] = stream
        mode_args = argparse.Namespace(**mode_args_dict)
        steps, details, breaking = run_mode_test(mode_args, levels)
        report_label = stream_mode_file_label(stream) if test_both_modes else ""
        files = write_reports(mode_args, steps, details, breaking, report_label)
        report_text = build_final_report(mode_args, steps, breaking, files)
        print("\n" + "=" * 72)
        print(report_text)
        print("=" * 72)
        return ModeRunResult(stream, steps, breaking, files)

    if args.parallel_stream_modes:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(run_and_report, stream) for stream in stream_modes]
            mode_runs = [future.result() for future in futures]
    else:
        for stream in stream_modes:
            mode_runs.append(run_and_report(stream))

    if test_both_modes:
        comparison_report = write_comparison_report(args, mode_runs)
        print(f"\n流式与非流式对比报告: {comparison_report}")

    return 0 if all(
        mode_run.steps and all(step.success_count > 0 for step in mode_run.steps)
        for mode_run in mode_runs
    ) else 1


if __name__ == "__main__":
    sys.exit(main())
