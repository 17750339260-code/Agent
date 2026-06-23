from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from email.utils import formatdate
from typing import Any

import requests
import urllib3
from urllib3.exceptions import InsecureRequestWarning


APP_KEY = os.getenv("APP_KEY", "1001300033")
SECRET_KEY = os.getenv("SECRET_KEY", "24e74daf74124b0b96c9cb113162a976")
# URL = os.getenv("GATEWAY_URL", "https://192.168.0.213:18300/ai-inference-gateway/predict")
URL = os.getenv("GATEWAY_URL", "https://10.10.65.213:18300/ai-inference-gateway/predict")
COMPONENT_CODE = os.getenv("COMPONENT_CODE", "04100565")
MODEL = os.getenv("MODEL", "Qwen3-VL-32B-Instruct")

SYSTEM_PROMPT = "你是一个严谨的电网面试助手，请严格遵守用户要求并仅输出合法结果。"
USER_PROMPT = """
# 角色定义
资深电网国企面试官，擅长从候选人**真实工作经历、业务成果、解决问题的方法**中提炼考察点，尤其擅长出"压力场景+短板验证"类行为面试题。

# 任务
基于下方提供的候选人简历数据、候选人优劣势，生成 **{question_count}** 道行为面试题及对应的STAR标准答案。
- 强制要求：必须生成 **{question_count}** 道题目
- 题目之间不得重复（场景、能力、压力点不重复）

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
专岗经验，管理经验，攻坚经历，激励星级，荣誉奖项，工作绩效，任职不稳，经历断层，专业匹配，业绩不明，惩处记录，家庭稳定

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
  ]
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
    {{
      "question": "你目前主要从事网络安全及信息运维工作，而本次应聘的是规划建设管理中心的技术质量管理专责。请结合你在2024年推动数字化应用或护网演习的经历，谈一谈你是如何将信息化手段应用到传统电网生产业务中的？如果未来在基建工程质量监督中遇到数据孤岛或监管滞后的问题，你会如何借鉴过往的中级/高级工技能与信息化经验来破局？",
      "skills": ["专岗经验", "技职水平"],
      "description": "基于专岗经验与技职水平，考察信息技术与电网业务融合的创新能力及前瞻性破局思维",
      "answer": "2024年借调营配指挥中心期间，面对全局网络安全漏洞频发、终端设备维护工单量大且人工处理效率低下的痛点，以及上级要求将网络安全提升至与电网安全同等重要位置的高压态势，我作为高级网络安全及信息运维管理员，主动牵头建立"全域全流程"管理体系以消除弱口令等安全隐患。在具体执行中，我梳理高频重复业务流程，自主设计并推广应用了4个RPA流程机器人和12个云景平台应用场景；针对通信光缆管控难的问题申报专项资金并制定日常运维机制；同时在"HW2024"实战攻防演习中统筹编制专项方案与技防措施清单，将被动防御转化为主动的数据监测联动处置。最终成功闭环486份终端维护工单，整改162项弱口令，圆满完成"三不一零"底线目标。这一经历让我深刻认识到技术质量监督的核心在于"用数据驱动管理"，若入职新岗位，我将把这种数字化思维引入基建质监，利用信息化平台打通工程进度与质量验收的数据壁垒，实现从"事后检查"向"全过程在线智能监控"的转变。"
    }},
    {{
      "question": "简历显示你在2025年赴昆明电科院跟班学习期间，协助完成了国家级科技项目的策划申报，实现了公司在国家自然科学基金赛道上的'零突破'。请详细复盘这项重大攻坚任务，在面对高规格、严要求的科研策划时，你作为基层单位派出的跟班人员，是如何克服专业壁垒、协调各方资源并最终达成目标的？",
      "skills": ["攻坚经历", "荣誉奖项"],
      "description": "基于攻坚经历与荣誉奖项，考察在国家级高规格项目中的跨层级协调与突破性执行能力",
      "answer": "2025年9月至2026年2月我在昆明电科院科创中心跟班学习时，正值"智能电网"国家科技重大专项及国家自然科学基金申报的关键期，面临时间紧、任务重且涉及多领域前沿技术交叉的极高要求。我的核心任务是高质量协助开展重大科技项目策划，重点配合专家进行可研评审与资料审查，并牵头推进国家自然科学基金项目的论证申报，力争实现历史性突破。面对专业跨度大的挑战，我主动发挥电气工程及其自动化专业背景与多年基层一线工作经验相结合的优势充当"理论"与"现场"的桥梁，积极对接各领域专家提前梳理申报痛点，协同完成10项国家级科技项目策划的资料打磨；在国家自然科学基金申报中，我牵头组织多轮内部论证，逐字逐句核对技术指标与应用场景以确保契合工程实际。最终我们协同完成10项国家级科技项目申报创公司历史新高，并成功实现国家自然科学基金赛道"零突破"获省公司科创中心书面表扬，这次攻坚经历极大锻炼了我统筹复杂项目、把控核心节点的能力，这正是优秀基建质监专责应对重大工程质量核查和高标准项目验收时不可或缺的核心素养。"
    }},
    {{
      "question": "你在计划生产部担任检修工时，曾负责配电自动化终端定值更改与离线设备处理，并实现了故障隔离准确率100%。请分享一次在现场巡维或终端消缺过程中，遇到的最棘手的技术难题或突发状况。你是如何快速定位问题、协调资源，并确保不影响整体供电可靠性的？",
      "skills": ["管理经验", "持证情况"],
      "description": "基于专业匹配缺口，考察现场突发技术问题中的快速定位、资源协调与保供电实战能力",
      "answer": "在从事计划生产部检修工期间，随着配网自动化设备增加，部分老旧FTU终端频繁出现离线或定值不匹配现象严重威胁区域供电可靠性，某次极端天气后辖区内多个终端集中告警导致现场排查难度极大。作为现场骨干，我必须迅速查明根本原因并完成定值精准更改与设备消缺，确保终端在线率达标且不引发二次停电或扩大故障范围。我第一时间启动应急响应，利用主站系统数据分析锁定异常点位与通信状态，随后带领团队赶赴现场采取"先保通信、后查本体"策略逐一排查光纤链路、电源模块及主板程序；针对共性问题现场制定了标准化消缺作业指导书规范后续操作，同时与调度端保持实时联动采用旁路代供等方式保障居民用电不受影响。最终不仅迅速恢复了所有终端正常运行，还将2022年配电自动化终端在线率提升至98.85%（优于年度指标1.27个百分点），故障隔离准确率达100%并节约时户数4102时·户，这段扎根现场的排故经验让我对配网设备的物理特性与运行工况有了直观认知，未来在做基建质量监督时我能更敏锐地发现施工安装工艺和设备选型等环节可能埋下的隐患。"
    }},
    {{
      "question": "你近年来多次获得'素质杯'数字化应用个人二等奖、职工创新技术集体三等奖等荣誉，并在各项工作中保持了较高的绩效评价。这些荣誉背后往往伴随着对现有工作流程的颠覆或优化。请具体讲述一个你主导或深度参与的职工创新项目，谈谈你是如何发现业务痛点的？该创新成果在实际应用中取得了什么成效？如果将该创新思维应用到当前的基建工程质量管控中，你有什么设想？",
      "skills": ["激励星级", "工作绩效"],
      "description": "基于工作绩效与创新荣誉，考察从业务痛点挖掘到成果转化的创新实践能力及思维迁移",
      "answer": "在日常网络安全与信息运维工作中，我发现大量基础台账核对与漏洞通报下发等工作依赖人工流转不仅耗时费力且极易因人为疏忽导致合规风险，同时安全生产业务升级急需轻量化数字化工具支撑。作为创新项目骨干，我立足岗位痛点牵头组建跨专业柔性团队深入调研需求，自主设计了基于云景平台的12个应用场景将分散的台账数据进行结构化整合，主导了从需求分析、原型测试到上线推广的全流程并建立了"发现问题-提出创意-落地验证"的创新孵化机制，进而将此模式复制到工器具研制等领域累计申报了8个职工创新项目。相关成果荣获临沧供电局"素质杯"数字化应用个人二等奖及云南电网职工创新技术集体三等奖，切实为基层减负并提升了数据准确性。对于基建质监而言这种"微创新"思维同样适用，例如可以开发基于移动端的质量缺陷随手拍与自动归类工具，或者利用AI图像识别技术辅助隐蔽工程的影像资料审查，用技术创新倒逼工程质量标准的刚性执行。"
    }},
    {{
      "question": "你的履历非常丰富，横跨工程造价、配网管理、检修试验、网络安全以及省级科研院所跟班学习等多个板块。虽然这体现了你极强的适应能力，但作为规划建设管理中心的技术质量管理专责，需要长期深耕基建领域。请坦诚谈谈，你频繁跨越不同专业序列的核心驱动力是什么？面对即将到来的基建质监新岗位，你如何保证不再出现'短期适应后再次寻求跨界'的情况，从而确保履职的长期稳定性？",
      "skills": ["任职不稳", "专业匹配"],
      "description": "基于任职不稳的劣势，考察职业规划清晰度、岗位忠诚度及长期扎根的决心",
      "answer": "自2012年参加工作以来我经历了从规划建设部造价岗到生产设备部科技岗再到安监、生技、营配指挥等多岗位的历练，每次岗位变动均是因为组织工作需要或个人在特定阶段遇到了能力瓶颈需要通过新环境拓宽视野。我需要向面试官清晰阐释我的职业发展底层逻辑以证明过往的"多面手"经历并非盲目跳槽而是为了构建复合型知识体系，同时给出令人信服的承诺表明我已找到值得长期奋斗的职业锚点。我的每一次跨界都是围绕"大电网安全与高质量发展"这一主线展开的，懂造价让我具备成本意识，懂配网和检修让我掌握设备全生命周期规律，懂网络安全和科创则赋予我数字化与前瞻性视角；如今我已36岁处于职业生涯的黄金成熟期且家庭稳定（妻子在本地任教父母均在本地），非常渴望将沉淀多年的复合经验在一个核心岗位上生根发芽，而基建质监正是能将工程管理底子、现场设备经验和数字化管理思维完美融合的"集大成"岗位。过去的广度积累是为了今天的深度发力，我深知基建质监工作需要耐得住寂寞守得住底线，已做好长远规划将以此次竞聘为新起点沉下心来钻研基建规程与质量标准致力于成为该领域的专家型专责，绝不会再轻易偏离这条专业深耕的道路。"
    }}
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


def make_headers(app_key: str, secret_key: str) -> dict[str, str]:
    x_date = formatdate(timeval=time.time(), localtime=False, usegmt=True)
    sign_text = f"x-date: {x_date}"
    signature = base64.b64encode(
        hmac.new(secret_key.encode("utf-8"), sign_text.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")

    return {
        "x-date": x_date,
        "authorization": (
            f'hmac username="{app_key}", '
            f'algorithm="hmac-sha256", '
            f'headers="x-date", '
            f'signature="{signature}"'
        ),
        "Content-Type": "application/json",
    }


def make_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "componentCode": args.component_code,
        "model": args.model,
        "messages": [
            {"role": "system", "content": args.system_prompt},
            {"role": "user", "content": args.user_prompt},
        ],
        "stream": args.stream,
    }


def pick_text(data: Any) -> str:
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        return "".join(pick_text(item) for item in data)
    if not isinstance(data, dict):
        return ""

    choices = data.get("choices")
    if isinstance(choices, list):
        text_parts: list[str] = []
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or {}
            message = choice.get("message") or {}
            if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                text_parts.append(delta["content"])
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                text_parts.append(message["content"])
            if isinstance(choice.get("text"), str):
                text_parts.append(choice["text"])
        if text_parts:
            return "".join(text_parts)

    for key in ("content", "text", "answer", "result", "output", "response", "data"):
        value = data.get(key)
        text = pick_text(value)
        if text:
            return text
    return ""


def parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def read_response(response: requests.Response, stream: bool, start: float) -> tuple[Any, int, int, float | None]:
    first_byte_ms = None
    response_bytes = 0
    stream_events = 0

    if not stream:
        body = b""
        for chunk in response.iter_content(chunk_size=8192):
            if not chunk:
                continue
            if first_byte_ms is None:
                first_byte_ms = (time.perf_counter() - start) * 1000
            response_bytes += len(chunk)
            body += chunk

        text = body.decode(response.encoding or "utf-8", errors="replace")
        return parse_json(text), response_bytes, stream_events, first_byte_ms

    raw_lines: list[str] = []
    text_parts: list[str] = []
    for raw_line in response.iter_lines(decode_unicode=False):
        if not raw_line:
            continue
        if first_byte_ms is None:
            first_byte_ms = (time.perf_counter() - start) * 1000

        response_bytes += len(raw_line)
        line = raw_line.decode(response.encoding or "utf-8", errors="replace").strip()
        raw_lines.append(line)

        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            continue

        stream_events += 1
        data = parse_json(line)
        text = pick_text(data)
        text_parts.append(text if text else line)

    return "".join(text_parts) if text_parts else "\n".join(raw_lines), response_bytes, stream_events, first_byte_ms


def fmt_ms(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2f} ms"


def request_once(args: argparse.Namespace) -> bool:
    if args.insecure:
        urllib3.disable_warnings(InsecureRequestWarning)

    payload = make_payload(args)
    if args.print_payload:
        print("请求参数:")
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    start = time.perf_counter()
    response = requests.post(
        args.url,
        headers=make_headers(args.app_key, args.secret_key),
        json=payload,
        verify=not args.insecure,
        stream=True,
        timeout=args.timeout,
    )

    try:
        header_ms = (time.perf_counter() - start) * 1000
        data, response_bytes, stream_events, first_byte_ms = read_response(response, args.stream, start)
        total_ms = (time.perf_counter() - start) * 1000

        print("\n响应指标:")
        print(f"  状态码: {response.status_code}")
        print(f"  响应头耗时: {fmt_ms(header_ms)}")
        print(f"  首包耗时: {fmt_ms(first_byte_ms)}")
        print(f"  总耗时: {fmt_ms(total_ms)}")
        print(f"  响应字节数: {response_bytes}")
        print(f"  流式事件数: {stream_events}")

        print("\n响应内容:")
        if isinstance(data, (dict, list)):
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(data)

        return response.ok
    finally:
        response.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Qwen3-VL 网关请求脚本")
    parser.add_argument("--app-key", default=APP_KEY)
    parser.add_argument("--secret-key", default=SECRET_KEY)
    parser.add_argument("--url", default=URL)
    parser.add_argument("--component-code", default=COMPONENT_CODE)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--system-prompt", default=SYSTEM_PROMPT)
    parser.add_argument("--user-prompt", default=USER_PROMPT)
    parser.add_argument("--stream", dest="stream", action="store_true", default=True)
    parser.add_argument("--no-stream", dest="stream", action="store_false")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--insecure", action="store_true", default=True)
    parser.add_argument("--verify-ssl", dest="insecure", action="store_false")
    parser.add_argument("--print-payload", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    try:
        sys.exit(0 if request_once(parse_args()) else 1)
    except requests.RequestException as exc:
        print(f"请求失败: {exc}", file=sys.stderr)
        sys.exit(1)
