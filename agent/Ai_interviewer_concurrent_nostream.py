#!/usr/bin/env python3
# cspell:disable
# pyright: reportMissingImports=false, reportAttributeAccessIssue=false
# 本文件包含完整 JSON、SHA-256 和 Base85 单文件资源，关闭拼写检查可避免将数据误判为代码错误。
""" 虎哥那边AI面试的脚本
校招/内招生产业务等价单文件模型测试工具。

业务基准：ai-interview-service Git commit e49bb35（策略化版本）。
核心推理、提示词、评分、反思重试和后处理源码原样内嵌；仅适配模型网关、
Java 回调捕获以及单文件资源加载。目标环境只需 Python 3.10+。
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import copy
import csv
import hashlib
import importlib
import json
import logging
import math
import os
from pathlib import Path
import shutil
import statistics
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import socket
import zipfile
import io
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from typing import Any

for _stream in (sys.stdout, sys.stderr):
    try:
        _reconfigure = getattr(_stream, "reconfigure", None)
        if callable(_reconfigure):
            _reconfigure(encoding="utf-8")
    except OSError:
        pass

ROOT = Path(__file__).resolve().parent
DEFAULT_OUT_DIR = ROOT / "outputs"
PRODUCTION_REVISION = "e49bb35"
DEFAULT_CONFIG = {
    "customer_code": "1001300038",
    "secret_key": "d26133d03af84b878b53c505b7cfbc9b",
    "url": "https://10.10.65.213:18300/ai-inference-gateway/v1/chat/completions",
    "model": "qwen3_235b",
    "component_code": "04101979",
    "timeout": 600,
}

# Tail percentiles are reported only when the sample count gives the tail a
# meaningful number of observations. Raw interpolated values remain available
# as *_observed fields for forensic comparison, but are never presented as SLA
# grade numbers below this threshold.
MIN_RELIABLE_TAIL_SAMPLES = 1000

# 六份研发测试 JSON 的完整原文。不得合并、精简或重新拼装。
EMBEDDED_FIXTURES_JSON: dict[tuple[str, str], str] = {
    ('campus', 'profile'): r'''{
  "resumeId": "modeltest-campus-1001",
  "name": "张三",
  "placeOfOrigin": "广东省广州市",
  "appliedCompany": "广东电网有限责任公司",
  "appliedDeptPath": "广州供电局/变电管理一所",
  "appliedPosition": "电气工程技术",
  "birthDate": "2001-06-15",
  "politicalStatus": "共青团员",
  "majorRank": "8/120",
  "currentEdu": {
    "university": "华南理工大学",
    "education": "本科",
    "major": "电气工程及其自动化",
    "university_category": "211"
  },
  "educational": [
    {
      "startDate": "2019-09",
      "endDate": "2023-06",
      "university": "华南理工大学",
      "education": "本科",
      "major": "电气工程及其自动化"
    }
  ],
  "foreignLanguageProficiency": [
    {
      "language": "英语",
      "level": "大学英语六级",
      "score": "520",
      "acquisitionDate": "2022-06-01"
    }
  ],
  "computerProficiency": [
    {
      "language": "计算机二级",
      "level": "二级",
      "acquisitionDate": "2021-09-01"
    }
  ],
  "workExperience": [
    {
      "startDate": "2022-07",
      "endDate": "2022-09",
      "company": "南方电网科学研究院",
      "position": "实习研究员",
      "description": "参与配电网可靠性分析课题，协助整理现场试验数据并撰写阶段性报告。"
    }
  ],
  "researchExperience": [
    {
      "startDate": "2021-03",
      "endDate": "2022-05",
      "projectName": "光伏并网谐波治理研究",
      "role": "核心成员",
      "description": "负责仿真建模与实验数据整理，完成论文初稿。"
    }
  ],
  "clubExperience": [
    {
      "startDate": "2020-09",
      "endDate": "2022-06",
      "clubName": "学生会科技部",
      "position": "部长",
      "description": "组织校园科技节，统筹报名与展项评审。"
    }
  ],
  "awardsAndHonors": [
    {
      "awardName": "校级一等奖学金",
      "awardLevel": "校级",
      "issuingAuthority": "华南理工大学",
      "awardDate": "2021-11-01"
    }
  ],
  "certificates": [
    {
      "certificateName": "电工进网作业许可证",
      "issuingAuthority": "电力监管机构",
      "level": "低压",
      "acquisitionDate": "2022-12-01"
    }
  ]
}
''',
    ('campus', 'questions'): r'''{
  "n": 3,
  "resumeInfo": {
    "resumeId": "modeltest-campus-1001",
    "name": "张三",
    "placeOfOrigin": "广东省广州市",
    "appliedCompany": "广东电网有限责任公司",
    "appliedDeptPath": "广州供电局/变电管理一所",
    "appliedPosition": "电气工程技术",
    "birthDate": "2001-06-15",
    "politicalStatus": "共青团员",
    "majorRank": "8/120",
    "currentEdu": {
      "university": "华南理工大学",
      "education": "本科",
      "major": "电气工程及其自动化",
      "university_category": "211"
    },
    "educational": [
      {
        "startDate": "2019-09",
        "endDate": "2023-06",
        "university": "华南理工大学",
        "education": "本科",
        "major": "电气工程及其自动化"
      }
    ],
    "foreignLanguageProficiency": [
      {
        "language": "英语",
        "level": "大学英语六级",
        "score": "520",
        "acquisitionDate": "2022-06-01"
      }
    ],
    "computerProficiency": [],
    "workExperience": [
      {
        "startDate": "2022-07",
        "endDate": "2022-09",
        "company": "南方电网科学研究院",
        "position": "实习研究员",
        "description": "参与配电网可靠性分析课题，协助整理现场试验数据。"
      }
    ],
    "researchExperience": [],
    "clubExperience": [],
    "awardsAndHonors": [
      {
        "awardName": "校级一等奖学金",
        "awardLevel": "校级",
        "issuingAuthority": "华南理工大学",
        "awardDate": "2021-11-01"
      }
    ],
    "certificates": []
  },
  "strengths": [
    "教育背景：毕业于华南理工大学电气工程及其自动化专业，具备扎实专业基础。",
    "实习经历：在南方电网科学研究院实习，接触配电网可靠性分析工作。"
  ],
  "weakness": [
    "空档时间：教育与实习衔接较紧，无明显空档。"
  ]
}
''',
    ('campus', 'report'): r'''{
  "resumeInfo": {
    "resumeId": "modeltest-campus-1001",
    "name": "张三",
    "placeOfOrigin": "广东省广州市",
    "appliedCompany": "广东电网有限责任公司",
    "appliedDeptPath": "广州供电局/变电管理一所",
    "appliedPosition": "电气工程技术",
    "birthDate": "2001-06-15",
    "politicalStatus": "共青团员",
    "majorRank": "8/120",
    "currentEdu": {
      "university": "华南理工大学",
      "education": "本科",
      "major": "电气工程及其自动化",
      "university_category": "211"
    },
    "educational": [
      {
        "startDate": "2019-09",
        "endDate": "2023-06",
        "university": "华南理工大学",
        "education": "本科",
        "major": "电气工程及其自动化"
      }
    ],
    "foreignLanguageProficiency": [],
    "computerProficiency": [],
    "workExperience": [
      {
        "startDate": "2022-07",
        "endDate": "2022-09",
        "company": "南方电网科学研究院",
        "position": "实习研究员",
        "description": "参与配电网可靠性分析课题。"
      }
    ],
    "researchExperience": [],
    "clubExperience": [],
    "awardsAndHonors": [],
    "certificates": []
  },
  "content": [
    "请先做一下自我介绍。",
    "我叫张三，本科就读于华南理工大学电气工程及其自动化专业，实习期间在南网科研院做过配电网可靠性分析。",
    "你在实习中具体负责哪些工作？遇到过什么困难？",
    "我主要负责试验数据整理和阶段性报告撰写，刚开始对现场术语不熟，后来通过请教导师和查阅规程逐步适应。",
    "为什么选择电网相关岗位？",
    "希望把专业所学应用到电力系统实际运行中，也认同电网服务社会的使命。"
  ],
  "dimensions": [
    {
      "tagName": "专业基础",
      "tagDescription": "电气工程基础知识掌握程度",
      "weight": 40
    },
    {
      "tagName": "表达沟通",
      "tagDescription": "表达清晰度与逻辑性",
      "weight": 30
    },
    {
      "tagName": "学习意愿",
      "tagDescription": "学习主动性与成长潜力",
      "weight": 30
    }
  ],
  "strengths": [
    "教育背景：毕业于华南理工大学电气工程及其自动化专业。"
  ],
  "weakness": [
    "空档时间：教育与实习衔接较紧，无明显空档。"
  ]
}
''',
    ('internal', 'profile'): r'''{
  "resumeId": "modeltest-internal-31001",
  "name": "李四",
  "appliedCompany": "广东电网有限责任公司",
  "appliedDeptPath": "广州供电局/安全监管部",
  "appliedPosition": "安全管理专职",
  "birthDate": "1988-03-20",
  "nativePlace": "广东省韶关市",
  "currentCompany": "广州供电局变电管理一所",
  "currentPosition": "安全员",
  "education": "本科",
  "university": "华北电力大学",
  "major": "电气工程及其自动化",
  "workYears": "12",
  "titleCertificate": "工程师",
  "highestTitleLevel": "中级",
  "skills": "注册安全工程师; 高压进网作业许可证",
  "foreignLanguage": "英语：大学英语四级",
  "joinPartyDate": "2015-07-01",
  "politicalStatus": "中共党员",
  "workAchievement": "主导变电站现场作业风险管控，建立隐患分级闭环机制，连续两年无人身事故；牵头完成二次设备隐患清零专项，获局级先进个人；参与编制省级作业安全技术指引两项。",
  "performance": "2023年A；2022年A；2021年B",
  "resumeAwards": "局级先进个人; 精益管理一等奖",
  "resumeCertifications": "注册安全工程师; 高压进网作业许可证",
  "discipline": "",
  "resumeWorkExperienceAllInformation": [
    {
      "company": "广州供电局变电管理一所",
      "position": "安全员",
      "startDate": "2018-03",
      "endDate": "至今",
      "description": "负责变电现场安全监管、隐患排查与作业许可管理。"
    },
    {
      "company": "韶关供电局",
      "position": "继电保护专责",
      "startDate": "2012-07",
      "endDate": "2018-02",
      "description": "负责继电保护定值管理与二次设备运维。"
    }
  ],
  "resumeWorkExperiences": "参与500kV变电站扩建配套二次改造项目。",
  "familyMembers": [
    {
      "relation": "配偶",
      "company": "某事业单位",
      "position": "职员"
    }
  ]
}
''',
    ('internal', 'questions'): r'''{
  "n": 3,
  "resume": {
    "resumeId": "modeltest-internal-31001",
    "name": "李四",
    "appliedCompany": "广东电网有限责任公司",
    "appliedDeptPath": "广州供电局/安全监管部",
    "appliedPosition": "安全管理专职",
    "birthDate": "1988-03-20",
    "nativePlace": "广东省韶关市",
    "currentCompany": "广州供电局变电管理一所",
    "currentPosition": "安全员",
    "education": "本科",
    "university": "华北电力大学",
    "major": "电气工程及其自动化",
    "workYears": "12",
    "titleCertificate": "工程师",
    "highestTitleLevel": "中级",
    "skills": "注册安全工程师",
    "foreignLanguage": "英语：大学英语四级",
    "joinPartyDate": "2015-07-01",
    "politicalStatus": "中共党员",
    "workAchievement": "主导变电站现场作业风险管控，建立隐患分级闭环机制，连续两年无人身事故。",
    "performance": "2023年A；2022年A；2021年B",
    "resumeAwards": "局级先进个人",
    "resumeCertifications": "注册安全工程师",
    "discipline": "",
    "resumeWorkExperienceAllInformation": [
      {
        "company": "广州供电局变电管理一所",
        "position": "安全员",
        "startDate": "2018-03",
        "endDate": "至今",
        "description": "负责变电现场安全监管、隐患排查与作业许可管理。"
      },
      {
        "company": "韶关供电局",
        "position": "继电保护专责",
        "startDate": "2012-07",
        "endDate": "2018-02",
        "description": "负责继电保护定值管理与二次设备运维。"
      }
    ],
    "resumeWorkExperiences": "",
    "familyMembers": []
  },
  "strengths": [
    "专岗经验：长期从事变电安全监管与继电保护相关工作。",
    "持证情况：持有注册安全工程师证书。"
  ],
  "weakness": [
    "任职不稳：近两段工作经历时长均超过5年，稳定性较好。"
  ],
  "job_title": "安全管理专职",
  "job_description": "负责安全生产监督管理、隐患排查治理与作业许可审核。"
}
''',
    ('internal', 'report'): r'''{
  "resumeId": "modeltest-internal-31001",
  "resumeInfo": {
    "resumeId": "modeltest-internal-31001",
    "name": "李四",
    "appliedCompany": "广东电网有限责任公司",
    "appliedDeptPath": "广州供电局/安全监管部",
    "appliedPosition": "安全管理专职",
    "birthDate": "1988-03-20",
    "nativePlace": "广东省韶关市",
    "currentCompany": "广州供电局变电管理一所",
    "currentPosition": "安全员",
    "education": "本科",
    "university": "华北电力大学",
    "major": "电气工程及其自动化",
    "workYears": "12",
    "titleCertificate": "工程师",
    "highestTitleLevel": "中级",
    "skills": "注册安全工程师",
    "foreignLanguage": "英语：大学英语四级",
    "joinPartyDate": "2015-07-01",
    "politicalStatus": "中共党员",
    "workAchievement": "主导变电站现场作业风险管控，建立隐患分级闭环机制，连续两年无人身事故。",
    "performance": "2023年A；2022年A；2021年B",
    "resumeAwards": "局级先进个人",
    "resumeCertifications": "注册安全工程师",
    "discipline": "",
    "resumeWorkExperienceAllInformation": [
      {
        "company": "广州供电局变电管理一所",
        "position": "安全员",
        "startDate": "2018-03",
        "endDate": "至今",
        "description": "负责变电现场安全监管。"
      }
    ],
    "resumeWorkExperiences": "",
    "familyMembers": []
  },
  "content": [
    "请介绍一下你近三年最有代表性的安全管控成果。",
    "我主导建立了隐患分级闭环机制，连续两年实现变电现场无人身事故，并推动二次设备隐患清零专项落地。",
    "面对作业许可冲突或违章苗头，你通常怎么处理？",
    "先停工确认风险，再按规程组织整改并复盘，必要时升级汇报，确保风险可控后再恢复作业。",
    "你如何看待从继电保护转安全监管的岗位变化？",
    "继电保护经历让我对二次系统和现场风险更敏感，转到安全监管后更能把技术细节落到管控措施上。"
  ],
  "dimension": [
    {
      "tagName": "安全专业能力",
      "tagDescription": "安全生产法规与现场管控能力",
      "weight": 40
    },
    {
      "tagName": "沟通协调",
      "tagDescription": "跨班组协调与问题推动能力",
      "weight": 30
    },
    {
      "tagName": "责任担当",
      "tagDescription": "关键时刻的责任意识与执行力",
      "weight": 30
    }
  ]
}
''',
}

# 原业务模块的 Git blob 内容哈希，用于证明内嵌源码未被改写。
ORIGINAL_SOURCE_HASHES = {
  "inference/analyze_service.py": "2b10c827a5defe29999c69e028570767b968d6766695ebbee75578ec40054589",
  "inference/evaluation_detect.py": "ea775b73b2019d12a7c44adbeb520712fc7da3d8657a934bb05d4a4fc6f2cdbf",
  "inference/profile_detect.py": "bf486986d7a79a80d63cd5c1aa19e7d9cc75ec206760f4abe2e05af0099240c6",
  "inference/prompts/__init__.py": "e1eff2c9ac4a688064b1d284318ddc8d91173f8b5a7352bc06f3b379f065df6a",
  "inference/prompts/campus/__init__.py": "74cb80eab275a7915380e0e0cd953888a7d4556f783803d61a9a5350eefd4e41",
  "inference/prompts/campus/evaluation.py": "78e32c95df7c621868c5dcf09b15c8e04b9478e3ea178a4bc3361a52ab314bf3",
  "inference/prompts/campus/interview.py": "1e3e19c299a08082a787d16d77cec7460c56c975cd4e7d1245efbb8b828c2181",
  "inference/prompts/campus/resume.py": "24329a5beba51b5edfd790c9087f7029a482994a4d417772fec81a6e7f9bedfa",
  "inference/prompts/campus/scoring.py": "ec9f6d76108b5d4be305122e09cad816ee062427bde40e8d8f32e27ae37613de",
  "inference/prompts/internal/__init__.py": "93c3909a56192cebbe06804b3dcc687fdaa4547d8b3e8a00111b1bc9f0760d20",
  "inference/prompts/internal/evaluation.py": "a4ccccb4fa8b166720d042bd08dcfc1952be1c5c8aea22a4ebc0844f946b8ddc",
  "inference/prompts/internal/interview.py": "8d8c063b6bae5ce6a4133f65a08a273f8abbd242e71eaa9511f6a39f85add00f",
  "inference/prompts/internal/resume.py": "200ef94d8a6006aa00fa8a4f9d632c4e25d61dd8e2cf2f0e0ae214d7bd7b715e",
  "inference/prompts/internal/scoring.py": "eae9d714c52186f39bb809dbdecc1cb165695bda1247d8a63684523359824928",
  "inference/prompts/shared.py": "75d7051b07e333e03e8ff049d55a0418024113a06682a07022f80799ac028096",
  "inference/question_detect.py": "96382361c482d07b5f353ffe82d4e6be378eeeb79779c6b180b0c84ea5597fec",
  "inference/strategy/__init__.py": "95c0509606a65b42a52899fa72a35fb1a05397f491b44a9743cd26eb76edbf30",
  "inference/strategy/base.py": "d7ae8d7182913c52fe8bf5de20355b9e287128e11f99554de9822b44779433b4",
  "inference/strategy/campus.py": "66897b1f935935bf0d3c62f8679741be6a15557d996d3f64ca882fe3e4abf943",
  "inference/strategy/internal.py": "099e0e9fca1d2d82345cce7c6febddfccc5a620c670d7795d5f92861a3d09bdb",
  "inference/weakness_detect.py": "a1ba1e1c837adc0663dce019c08170aa0cf25872804795b31d452e3d1b15efb0",
  "inference/work_experience_detect.py": "99cc9c83114a73a3c97e67b28d3547aa9dbd7171a145170e14540cd7f914cbf3",
  "util/text_util.py": "5737cbc1ecb44d81f9bf2e39d2fcebf8a0062c7f8bae6feb0770e5905b032f41",
  "data/func_dept_name.json": "896c72dbc6d28563689c134f2f449e8b85be20cd9a3ab0438524bf4ee6a042e2",
  "data/work_station_director.json": "2a4d8b6015580babb08f32a3e985d45ec2e21f8f27d75b4f7bcb183c93a1c602"
}
BUSINESS_PACKAGE_SHA256 = "d65492a04f97353275890b1d664290d784174e84e0bc7dc4ae3d28606d203448"
# 下方是 ZIP 业务包的 Base85 数据，不是可读代码或英文单词。
# noinspection SpellCheckingInspection
# cspell:disable
BUSINESS_PACKAGE_B85 = (
    'P)h>@6aWAK2mk<F3|*4bZJt~O002o8000~S003!jW@U0^ZewLHVQyh;d3t4Eb7gXNX=7zBaCzleOK;mo5We$QEZ7SfNUV|G1n7`M'
    '3*_FLVGy*qa@bI$vRtKBRRXHh*p?rX8u$^|t{bN@>K3pa14ZjvO8sA2k(5sU3!UXlq%1i_n;zm4hDq-3Jic#dW{2W=p1XH{@a@a~'
    '>8884?yMYm-7UBI%sE@&IJedA{l42fZ}iT8a=Hud@uAy1^4g1T^U!I1@9li;JY^5g(i?a4^;LI=%Ob_98U<M~6utTdmdnH}YC6uL'
    '`0`HbPIJ-Q=r~VW&hnzW-;Ramuz`s}vHYllEvlIYsIflB#LA(-zrps&;N^3twc;IbxChJL#&_=W7;%EjCZP$N$m?{S_OiQj=zOyh'
    'N^x$Z?(Q`LB!l`+|8mJY-tsmN-J=7i+v+!eby{1_>2J=(hP%4k|LVDS)^YY%0rTMLy7%V9dA0`Zc&{${r|W;#7dRex)Jn`S`phzo'
    '=$UFI%tbG{IX8z1xXYg8lBq@$6{_hH!B$15azWAcS*7rhEsLJHA~8!yGm4mCqktv+SkWtrPmzi#E>ILIDww4*!4I%uX^*iyTsEp8'
    'W;iV#Qy35*Z-g}}AMWdj_}IN?IOMT2qe?BxY|mAP-cUIj<2V^OL-~j~3GRZ(aoh*!kInWi_yZsxjxrZy*X@DGskk^kNCX!AB`j-d'
    '4g!K^5W#-z$+M<f%^_7Q_+tf9(Wp$TWt>42m&z<yD+J0*Q8yI|(J>`Wi8+p?Jmh0ou^K=057vUR6BL|Yrvqi~s=Ms$9eEd*-qEUi'
    '_M&%w+`nwY%ByaJ^EKQfb5@{>T}k6Plq?DEi>3bQBB=8=j(Qi(LA~Lw{utB}rYm&DzsC`tPX)J-R4)1}S+q-#8m3U>L$0zRSJ~`z'
    'z}9^=UyFIOXHf6WCAbh+&Y@bQU1C~gLn&cf7#S>>Dz-&FH2IdDNpDIf29w%~+m@0O&!pR&oD)GOgMpoLvZ_$!U8HAlvL}!A_aRA%'
    'roy5kAMurq_{xTSO-fb%T8`I1m;G-t*w;u&HAx2RCwvqWPIV#P!(=wcgA%|Oc!STNkHicEo7&ZUI_o2=S}`)L7%I{%&9JBfr@Ii0'
    'X8d3g{YgG{gomOZiVgi|*DNlYgrTe$42h17P|tKict%XgFu!D7vzWm2Olk_x!>~;8Je#usU0M=sAq~Wb-lWW=U1QgyNEed|=!(_@'
    'YdkNqo<=K=m<eeLg&7m!qhV5Jdg$eKi)j>+qN+k5ERJ0ob0I0^GB#9!IhtNN1}FfE1ygIUM{L-VH#>u+b?0Oa5;up!x5j$DmfSx('
    '6V0@n`5N2G*obKJi4cmb&h>3ib8G?0(=(#T*qj(?Shp~m;wD1~K4}8yuexjQUft^~2e{y?X0X)C*U}!PoGmL=C~m9{)c7dZ`5a0V'
    '$~$*Lj}U}c%@*xgR;`rE7D%#;ll&JURF^@}!b4)gnOxAc{3i;4$UtnU*r55l;tgn=-TnUSCr)bte1{U^YfU*^Av&`eml0OuqVEiu'
    'O(vo>8kgb)BPYVp-vnT$WcY;<U=JO@^95X{=wtr|v+Uyl=hAR6Ato8(@lT21G;m&>^?zoCk(E9Hi<0aYMOn6C4BHS5pkHQm<Ub`9'
    'Jj6j1&BC{?Goj&^?*F0{384OG4M_60f%mKRpxJ<$n|lqaSPwy;LO_XD!mPr>O=AB9@f~*~=*v`}P)oenV>^{ezgmp%Pu|{xU|lI&'
    'SRI2DWk;E~sgx#5kAFM>vVMKr-TozCOWMducZ;^g|GgUb&z%bjW4dFRSS4?C6(+~lP#_tGppcGqO^Ko!&x}^rzW`860|XQR000O8'
    '09*`R`hD2is1pDH_Bj9m9smFUX>Mj^a%FB~WiMrRVQh6_bZKvHUu0!;Wn*+MaCyZ$YjYDxlHc_!IyA9?1V&>RCgM(e-iW!m*}J`%'
    'n7i2FK3sr9+emHPi!6!mHXff=5i;N>esS2?#x`KU*u!AUfCtO6vH#1W>TZ4FFI;9-b$9hc2IBVO95cAPD=RBAD>Ew}<#;?Evknf)'
    '?3i7<-8|eeSEk6sS}fK)n3j|qa=u?S)M6o38kEeLSu%6~!{H>E9452Bd^mg%J1eoN=EOR=J7LW|GRJ16Y_YGTDZNTTS8pg84@V*i'
    '@Xf<TGVzp597`n);0(!pc$*wFJ{*p<st@erQEThIHNOr^HIJ4*9FD^bv$|nEKK!y-pR(V+A!8%v!&(1aO2sZ|JO>7FeDN{axovN+'
    'S=$S3(Z+%6nA%q=YKHV@y;yLb^Tn%I)xuTh)lmBs_qkV7<Qxu)z=|fxIzDCkiaBSX-%#@@P0{=FMkXug^H=2TH3V~>V?98>q!xM<'
    'O(|rR)b#<Skjds%rC>-L1^uKc*ZURSVBi#~q5NcI@X3Ko#3cAB$(pWYWHp1lbbxpiK&QxnK_1A+y4E2TidvtXSN}`N<bWsGnqnbS'
    'k_YoeIoBce<YmKv^}<6GJIAH>_w~uzV26|p(ytquY$#U;Q&(hNaVYzvlGXZE<3~Ogi)BDcQkNsE6#NE_Fl6njVr25l4JFS8QU0la'
    '`tBe9<Dcn_l&%=x<7h$z1rmyy)GHTqdDsE9AW=~MuBR#4{H+}V#17li<!x;0d-wn}Wk9{Mimt<_jGEH{q+E$*zWwIgfB7zR@#6RJ'
    '{`Z}ou~_U2sVnLy$|fQKW3iml)81TAf%;&-WJB(d91*93a_x`|rLUyGOTVV1VJnNW0TgzoI+IKcpz5vFCw6UKB&b<GFn=AgHt)nJ'
    '%s1LqE;Rh{)-vxb+e3@is{``z4l^Mze`ZDSul1~9*QVZAhnV@n16h7(@7y9QJ7l$LZH>2vr>u9|4i)sb+5@TxS`Nb14H=Zj&t^`|'
    '8cB~5LmTv7@J9d!CQ;i{sHeJ!CF#yke#$D~CcdM;K%{<LRAm%RD{6_J`1|UtJ-SQozH7}b*|pmH>RoXua6aZ@4L9}8{J;MAZ$HG7'
    '0jfaw1tv<06-=WDNJSK6`VrZ<Lq>+l_I_F_3quva7896LbHG|67Qu1~)HlJg{vjt20k+TvO#L*lOc0K5S2E0$z3~)WFd5si9^Cdw'
    '_E=JLE<=2?J)l@S*_$g>>tL+4_?jH8xw8L5P9ocrWO&m$Y>;QK@Z=d<F}I$W;~Vzcq(Cp1KYgUq>xlzzXEg;K(b=<?9CTd*jotAu'
    'Yh{>Sk;q{cc1k=GrN2l_`@71nt1|{mN~R$9DK57Is?jS^r{eLM?bpCX8W}uD(AB`N6#kPCm9(U4N-lFv8AMle34TlH+~7CZwoaX!'
    '-yx+`FnK+ZMEUv_IE(PVlRQ_|?XJQxA~tcsfJFq>DHTfzO2!Yxg2K7v`|9axm_9#JB`=qB1$QMtYhczT7iD+YOb2AG0C4UeH4hFT'
    'N-?*dl9f&J?6x&K(yVWRg4la+;IxNhpX|K`2SkpBr7|ZVo$BnV=y3Q)8*dvK2vh|)Xs10Bk6Uv?;9|+dBS$lQZVY0849);_f-#D+'
    '6`Z33Ob>vhU$7zP7{D%gN9U`b^Pqdg5Jd^2^w}z763mr6yH8%-w6?2cdc>UHF{ie<V|ez;-dzWGKn>#r^59Gj`na%Id>#)*;~n6c'
    'l5K<~VEJTu`Bx4fD1S?-iEH60B7KU)`~-r~1m2@hQpSJaB#lJn>EsnPuNtbNM@ip}3PJdnx;+ZB#e#tmYs3^j6qw-OEe`X^JLN4W'
    '>=OTY@(#7xoS;u}77%v@TuN(c%4%#h>kaGY1{qth*ES<|$s_Jkcg%mv`Mg8x$!+GY;9$eJ@Gp`p-L7(8DJ0yTNLFBwqh&)5uV8+w'
    'YjMFHe<A&Kt_uJ0G>FVjSsOdx10jWD$Cf?4M>Dr^1}6r3O5v)}i-A9Gy?eNc1B^P|oiw1x*9zbuLzBb-XOci;S9Gxa&5wVS%=@F}'
    '%1is*OY`w4869aYY+GA%q_*E?j;K(M;33wRP)I<HD=1_`EhUmkI1|rxrenTcqoFs=Yal<$6g3s{jtr((3Cx-BA&sncU__CQ&{6!s'
    'bSDFW05zx3C=GidNWyUVXd#}aFY%6OxYo_5<lYMef~R&sc-W0hArL&pLok(C*n4nZfWS~v=rS5sXD4uTBoRptJ2){?3=Q|FKV^81'
    '-sOh=&QJ5*V+f|LxnIe|GQ@Gs`UAHA=HoTk^Je`X*?tRl2M?`<&F1koobsnn+qJFMqc@-;WMnA;I+Hwoy4l!(WCmmkAv+oSg={Tg'
    '1~NMda83|EL}s3{2bfP9GoV?mCyVc^H~j?F*UiU=t)XAZ_L#kKn~W`CZ9{}Omjc_m2k{`MdV8%kI)hk`XTj!ho$YaJ`7VB$U2n}{'
    '?gQilXJn;eF4d45%7-~#HCM(tFSQY%+N0DV+-1d@+l2Ir0em=I_7QYGg`u%IzuP=swdR)~xd0gg-sb)|;;QZ2wN;jD;OpLc^WBPd'
    'Fat+5yfVtnl^Vd~Yoh@k10KwUx2*-r%<F~byD7(wOoF8AQ{>4*GWM9xo-&``H1E9R&vW~*Be?$UnbW6(WPSy5dwm2!LThx2Jez_A'
    '&7C=xGm;TlWQpz%4cuUVX4YHP2d%XRnR-KJLM&fMNsMxsZ@wDELelV9^W8Cs$)`)yg^-zFAXN*Z_!^`(^TIy5M@AdK72Oi%h{^0U'
    'D3aBvF;zrPCb8o5d<ag1*60Ld7G428)gb=bda??EkX_p~7pGCJe9J-f$=9IU6I80-=)PAl6X`du$%Sny@=&B_4;nQMu*ITL!m~l@'
    '!@A&q*CR&a!4zRYD;BOI{MB9qBYW4wIXOo(34Sv8Qws90ybLG3h%|YUE8!DEARj-_seqWDb0zqHz$0G*e|<i8;cexPm>r_gu}nCG'
    '0O!YQ37B#-1eelZcXoDHQmK>}76_ZdS80FV(HfznB_AiitPh*J3t%)rgRn$<IwF<#y6hbiZWMtPI;4=%%ElAI8p$X$5P+6~`tM?I'
    '!x*0$E+uh%k<fWnM{4;z7VPrRfneA9DJTOOFFK5ePi<65R6JBgjw)IKufu!<<f|f+EGUPJ=tRSoeG}z^KA>oyFb#r$>ol&C+Z%+)'
    '-5IRAuu>!0UIcGfzFxUpAl0Q-bsP>E{2;@Jcyu)GQ!!o+qH$pQa-l3N6O|)jxZ*)LOttZr?p_E}{zeHN#<CEixbZ8?As~(~W3?!u'
    'BvTn$GRkBsEZytC#b`p-by4dVODwE*g>hw^*WEGf!(9WV)}3b{-+eZUhMdpn*`lVvr%oJ|OKv&=pDuMrg_ZG=qJi$q05*70lU2h-'
    'sP_T|*<v5$NbWaHfe#Sf=V(P8zFw$^F+RjetX*S`?~|jU6A~d-WD|kS=}pLs#HyY=5VUEZm&o?Ynw&KhEz?&t)EhA1x(n_}UCZ|s'
    '$-9JSv4)7rjIWV`p0Pvf;Z2*aavW>(3Kc~q8Q7^y5pwKisWCG<HrHRih){e%AS^QRCwk{G2=W?XSm>%hCo@xKb(Mu0*xdS<)I|)g'
    ')IVM9zJ7(EB>ykc7nKwY33Oe~w-LMR>|$aLc%@&>=X`8vN>5(N8ng|@IAW(ISQlrFXNG(^ak9=*Md|GC@OL_*&<H+KI0RTO#1at9'
    'WSIA)fjgu_trwGSI%L=Oz@u2h8|?7skTlTeLFU-1xwX#PJAjIBGS2~o$n<@6HjLtZ^{xw04zgSEPp0cI^uB3P;s{JVY>=_Hj9ivz'
    'i3q@XFv3_2PWE0i3lMW6F%4vyk)!rrq<@}9`7AMlY>Nfo*7B}7z1}=pHK(>QC*OWzZ`@?*Aodl;9=DdBvFxl_KY$@D{2%OzXd#zr'
    '9t@i^GxqTgn{G|-*ss>$TqDa5(cx2B+ef#^4rYSp$_pS2gbbskG3TV7L+098^XLw1X&^^yn~0F+L1gkir~>3lkUxq`L+V2-t`Au1'
    '@|C%|h8l6_6)*;7OlJ1}^xHqdL+imDOfwfA0~_Z27+Vsg%%)-eYHPuI`Mg<w?b8T03>rudXRvHUwGj)H;7r7%rCh!o$Aj#0JbgLd'
    ')(u>acU)#>N8z|~()@}k&AH!(znzcx_SNNh#VdEl#X3XCM8V`g#gM(4V}#pCMxl@Jh9R_{=jhzYJn{@UJ>Syp{a>^L>_M}o?$5W%'
    'C%D2dB-hNy@Cs?nv;FWov5+5S`s7lm(iJE3x2&NZYx54ROU>T`!^RhXtPwXChOI|$D9EdIduTE^-mH(e*7mWunx-$-avfg+P+-NN'
    'ykz}8t)?ySlCfoc2|$tAP4aA%+#6~w>{_c;dt`;K>wW_BpV_f@pMNQSvbT50yLrzuS7ImSjE*>>Q^v?W34d&pLIx}R;fbHHS{&RD'
    'nYjxt%3Pc_7mt`p#bcqc#TVsuN78vhKp}<_%v)nfGW$y)B*9w)G^7_#B}@o76{o~SPjy%PK@WOf=h_f!$bLM;@x%M0%8XOqJ~7U5'
    'Yv9~>hz+67v3ReR9%=6aIX-K5K7#6&D>T#>{B0-(1uRba{Y0kR8vXy0DX-SwjwuJi$CUH&M5e-$V}9p^D@+N$L5LD(zR#6r2P~ZI'
    '@OM7k$r1d0=F&d7u%%s|`O?1rNyO5a-{BR8c6YK8qF4f)B<a-+^TADw0T!pNwQXt$$8oV~cQSU7^8m3NyMwy*bh@?lbL+vZdGl#J'
    'oDN}{+D-coMLjf16Ia?%_jQSQs+*xl6{`vbchaviZB8@CbBgHF!S;{bpEvYi>4_UekE%d0;Q$2+B$&c`T~D+fMjCzxikw-#k8!sS'
    '5>E!YmAU>bz3>%<(j5%p6-VMhoO-smSj<Nu>@R@WG#!%EKrV_uee$1+TGT8$5HVq(s9nnpDq#FkvvBl3aWF)@I1(aWE|v0wnNl$l'
    'Cl0Dg?%#{RLX=p-<01UcB@WaixiA>D3J3X?K}0a_;Alf7ANEt>FNmr}K2nK#AqW7seG$i5ktJevMKkpF7bU)hVYNqP>Y+!>mj`4m'
    'cLIWM=s2I~n+bkiK1VNDmFeB5X1!5%HL*gro|EYjJnU=p&H968W4g>Vu~JT)!q4MX0AXF6Q_>lxZ}b`y(>x`wE7GY`6$HJr4oOVe'
    'Q?N?&U>X~0`@5|r2r?!ivS}^8HMf6l9vnbCiE9Nmwo(>%Nw_P8ug+4C2*pDjo~ImQOY-F!q;B@mV%b&73hRzx{&hS`j;f3-)?91W'
    'rx`f!plvOZl}#jiVvP*Hv4`d&SNn>t)~vt4PTb6Wa<ovU+ED?vSO?CW9718RZAV7v17iUKWx!3gADZ{3%!#Uf>z!R2YVJR$QUtLB'
    'Ss*j>{?GRG9?G2W{TZ0KHBM%4Ta68S>Z$!=90F;Gz)#Wb;6Qu@cE<<e$*~3S{MeU;NPT(_Ca|EnjTA31z98iuU&J^RQfsz_uhs#d'
    'wNbNnYm9m(A|RbiEF-(~yUlm^g2H^%z@ot9erp(oZyk)-6F1qE7KWDCon;jb`*^uoubNws#nxLdCPTCZ519m0>N9-n&+tuQzb_;&'
    'rO%)1><-7xyedmWW)Y(pxn4;5uBMCUlUtb$oJ(q7o2H0&RAY@-sJHp9{kUj5q>uEI;_UtfNQ(@wGb+vTdqAZ%T?5gwyBEQ`auC=7'
    '9l;ROBl>e<$msa}eRWnOhE;^@<2kbY@O|~}<wBX$5bOgW4eZK&WH<`LzDU%&qe3g=m4KfcP%syB5?%mi?$MC1{=m=kcrq=y-x)Z3'
    '?o85j5oH8fqPrP^n;9G=<FtJ|+5<PPbk<Y~Hy?nI3=cybME-Ci>h7J&x&t))E6Qx!yqT<X04$`wPKGD<$v?TXJ>k%q4lNz-3dx8I'
    'AqA&7fyYj<zmQ8nvX=m~31=2cuftmip3g^IwBnsWZgPOk|JMmYrnr?xgaOg&$FEQwEr9OP#tmn#BK>vbzooLkN9D`1ODo>hDhEi5'
    'zRbtNU$HaOor1$f0fDr+LgWHt3&<6AG_2Pv3Q*Gyp0|qxzQT}|kJ+r~<J^mFa3r;shMDC`^a7ic?2Fu=>P})6;e{Y9Jay6^zXI$U'
    'iNx;axwn5sYJ&007ZRo~^JDBxV^{H_?nruF!l(;nz8NYO5W(})i|33@5k*q^2kGxRg{${2>aNh$DSAQD!IqBf6lc+2M5|Goe&;2='
    '$=&R)LfY!2*gte&V__T4_A1$HdgO&Su~W%%Q124`!Cu6rHn*1<k+DWVJKfIc4AeiN5^}gSrE}8-6r;#c`@0|RA2Fbqs8`{bgv_(u'
    '1vx5N3XT%xON#+Qvp!)?th*oZxXR@E`%1cVC4s(h%ju9n;o%6)$a+>)yM8Z&p`6rAyi&{!V)xv`D*7bnkI|eGPTBjzlC*cdpZ0ul'
    'arh|Uak2Dk`A`n+V#dKlc*T?ppn@rAH&kVSAAboSQ5_U}DhPC9|8#n@RUL9Fk&||H!CZI?Uflb;3+b%y7JoW*uBW0qZsH@Y|A>}9'
    'zs>vEjeggl^1{WKsJ|d-_jeh>4za(^IN3$F=7z}L>(=OmId=ry{tr+~0|XQR000O809*`R<cozlP@w<-M0^4O8vp<RX>Mj^a%FB~'
    'WiN1YZ)Rz1WnW}vbY)|7E^v9=z590)SC%mPyZ?$BW%^nQM%d)h8FxrlzD{N)ck<}zWNznM#;p;y44TNYTe3qK%d=#_Py8YQ4A=$?'
    'HW0uM0s+6h*1i8pT2+#M>VM(x$2q6YsVd1bke=x;D~VNA=ef^5`|Q2XeuP4yy5dUSo*XJ(+Op40*&{>FjcI3Y!X8_wt1E0xTJdx|'
    'b>=@4v1}sqO|mT!?LK2UQ`7d;mA`C{+v9`w^uxbwU#~lC#j|J9Z82H8JJZ#XOeLBv@x$VV?ZG8`>JF5~0&R(GHg>uz^K~rVb}E_p'
    'CefKl_cUAf^QFShoHH_QuRVsIif6a6yc$MNCXqhba|&uE;$NqsiDs*?ebpYjV~_1|Cs@2Ko@(n&#d{Lm!I?xnlZCqRo@BNs*_M^P'
    '*c(sF*QSeGH%luESnWh_GSwdINJ3|^?)aHhSG>L1a^~{Itr6$)nlrKD+&FJ<Z9>rxPQqZ5ZI(SX;pFF?xlMcIS#fsV9(-aC??BnV'
    'Y>#xEK$s=|oFB->)5)IXe<rNr^5|c-N9#fW^<-yvSEk4Mk8D?3{_e`kUzvpb)syT@)OBRKI<59(M@K4o!V(|8Nc^cck#0+T8t-X4'
    'mB`e|3aPG>CleW14*V7C>}r=EdV7+oXeN>EP4ysnQYYeVUt=-(n|n#7I}#aeH~Od3iFB+jm4tc9KIv5kJI?lai_7JRwI_NKZK_JR'
    '&VqmVT`ZY~B{-2xC40`mg0{y_#=El(76ZLAnaw8CC)KhdhIDrKH0bp^;+@IV87$g~vzArGYqxD#HNu0WnfD~V?TO(pb7b%~6NjeL'
    'UFigksV$X=ryHzpKp1$DjNt+`Sm~}zXFLT<7;A?GNoSK?X^9{_!VW;T9)OPjv(?d?O2w#qZd(pZgB{{%;@j>-CW+61ApGLX-+%Pk'
    'KYjVnu@67~^rO$d`1teBzG$%i@X-hV^4UjUe4)RD=Idgx_SP|p0a5rHhPQR?i4H3p??}+3VmP{N81@3}sYo+*0*G1I*(+YUPk0GS'
    '-5pP6Ec^Bidwa8Z{k-*mfARTeR_Wdy*k!Q&OT!QBvHOL+6?<{QUSBWW8?Zk4<WsA(`@KE9S-y0ww7QCzjt%!@&TtR-PbSgRn@Lj_'
    'Q89>!Sd6wrH}8okcJ*PRt*brpQ6|%sacg(Svsrq0%_`15btadzhAeyWap~!j_4CkoR=`AfsQv-ba_9xsHpC}_mNs~NmuLs@YM<o{'
    'f~P0z7_<C{{s5)~PR$th#!YQr_S$@D^|CYaz`3$)kE}7A3p*3Vot0MuY9tB?w~1tj1ysK)YrSVRx^I0wRq3&%nHp$~7=<f?!3Pd%'
    'H=fKU0Eb_*e!i4{;eP|^_Di)`SJ#$;aay1E_H_65{A+I<DUjMt?tUmey<Z-lD&0G4uih+feec|u!@VJ>TZa_`;+X7-#lqP{sw3jU'
    '0w1FY2*S88GrbA(6HrZ{#_*}7715>z>VE@dq~}ZnG@EV9Bnh^W1}hqkwqhOr+H8dqxYi*nXN6A3Gie~bA+zOE$#jp~Fl-%X`3|1P'
    'i<_|Z3)|Q2x%-9n3x)N3X=kOhw_}gp0&KLH^un3>&|cehrx$N;kI_(Rehu0<fcWrG#ZT;(enQuwkOm0=m-BoJx?xC`XI9F$uObt2'
    '_bdQYwRo(aCRPs|$2I5j)56a8_Q;~Wa=oy=Sy-Pa=dYLch9y<<KrtHc?oOoJ!yXt$BKlsC->hTDBn(Z=HCOB(<Ed;yuTS&yS0HH1'
    '0pLT#1s<PrZC#lRoC<Wb5Re&Fti$giv<x`eYePDs6}PXHRxbhoDl8LFcA)RD5yT>o4;Qu;br`|0(_KB<K+Oq=Ew5FeLUiWgDjrXV'
    'qW{sAOoux{hpoO-{UNIZsLUxi0JSE4^s|HZ$Di}lKVQG~m-Q*Dus$Y`P1{e51=j!j9H#OJzS(<&<%KPt$Di{T>-yZ0^w*VEp99iD'
    'LFdr|H&R$%F6XDQ+36j7<wjv^345X1#j_iT=j#*p?o4@L#@U|wbN(!GZgq8^e(+W7_aFV^Cm;RxOE_|4zyJK-KKt+snCVg6DZnn;'
    'i`SXHz#AA8@PhW>W9tnI`&~N+rxK#=^uzMqEAR#xLOz;f@!p;;;6A^N^<;X}ZE;-dY`BXE3r#_6kz^WQw8wkm&A=75^$-s9^meBb'
    'itf<g-*shs4hu%R*}}#T3naqz2W|sawKr{Pomn4z_8}1F?l`e9SzIBcU_68<HF_9{kSO2GL$OZ5i4n3V1`3;(;NQ}{^Y+Mhp$1V+'
    '|0+J=cww4WPnW-%=;R|gh-T|Rop=lE+tt5ifqViuzD;)Gt|rD3h#^)u1^gh0E`UME$0Lcf{?yym1Lfg^mF<oLV50`ap(i57jr7Fp'
    'Bz6HEi0!?d-Py3aehn5di@h0OG_!5V<S`-^+QyH4ok(=YI)Q0~zJZN|!`pYX*VpsjYNJinMw<#ZY&~Fgz4h)fcm@BfFYoQuTTq}r'
    '&M)C?KAp&fBlPh<`BS4-{vYbaIC!vtcS)d4NYQ+Qa;M3AxDC1BlS!O(eG81DunF5})SjIM$lHU<h4pjx;KRbkJS<0R-_eG*`|GJ1'
    ';$ke@@hwyft4Y>}-*4vC%7KW6f65CR#o3jdb7jL>7|A(fOZLUF97uD;`xkP>CtD!P<?M|o_S9Mq2%5clH|N|Mwugsu^=_LW!If5a'
    'bIzrWa{g8hs3Vlj6;~FV$$Pod9*BcabIzSSkb#`@<5qEcC|6kj9=gpHr<RLb=X0gifx^b!2sbF8gs?}B9#$m^;$wRfP$pa-uD1@k'
    ';5cN}N1}a=FyZ&<gPJgYsBiE<T3hL4djjOqb`VJrrk&A~nXcaMa8m@Sjf>RUk>)9RluPPZ?>39l36$0QIK(Z~>GVI9NQd1DfHu~f'
    'h%?^T?^#Vp&BLLs3;3-xur2DXAZXQ4N~3uZx8iHoZ%-vaXocZWpB9|NNFuCwDwBw}pRw@x1i6j-JlWgTn@ye3C!x(4mdZ1Z{f7OT'
    '^^dTyR?n$~1p*`Rc@{5gRtVx&FP`|ST`LVkfzrvWmF?{S!k`-B+ObZOAfj0))^;jvbY1~*-s;EKPzRWjcsH<8xJr=*??hBn5$^#m'
    'd){xVqMXDyS<OJa-*)w;+N~1_iGd+2EDjRDT|^Co8pBsQR@zYr(uZYsW#D1(D6#KsyfcBQce3YH1L;Ljj_$GAPT}JJoWEpczfN{T'
    'kI|FS1|A0ypOE#9euRV!)cUOJw1qpDXgdgADWLASnz-Iw8CZujthk1kn$Xt6D=Ah*pHT$YuJY2UHad?Q$N)_?SjHakEM(`tn+ydu'
    'pa4gs*&gUA9P#cxaVlv~EzKyS!6;2qpqIc#57C}CCfVHTO44)y9y(?<saNeu!M5Ugz(`OpDT)cJcQtY%UM1i_wCVw|J6-yX%8h6;'
    '-O*)knouD7)NK7KYt{cMTaSzY{SE&r8woWSRn-t1OnelCut6gn4Q^p=pVesxvIejQhKFSDl&-0_^8{dQd#oqflS*X6inSv~E|tvo'
    'C_au=t;nnn0&BVqoH`y?Q_ta0Q_|1R`MIWOarB4MPv`By`}RfPaBsqC1_#$)wy*!3pI15<>Ww72It`bocQ04LW!<wa6F<$P)y}wh'
    'DizMuA8$X@68&?2p!In6^>E7{q08puX*}28XzY));A1PCZG^>cFWeNPeD4WtC3^@;)x&?Pr%03tS~wJr00RwNa>&>QN0CiIeBr|Q'
    'bhME+FE@@;OUj9O^cM6^3R_&iE@1K%Rt?Vf&Y(qWAr`!aSakSbYpZ7!^_8=y#_YjoC~RFBhE)WSps+bv*t`HjNa^lSaTZvTMd!*g'
    'h#YeDsMp#SkBaizb)!}?lV(u2fy|doCERV3sSm^QMfO|2aP;-}BjNXt0WYSnwx=+cjpY6wco}U((J$)Uvj|ZsrmZpmJkbSbH>{Ny'
    'tU|6=j5O3u#p?LgKrm|0L>vOF(h`UnP(=fO1pH0($qL!M);$l5b8+)t@!_q~*jdkRhflBsh27cWrJtP9e0gx9G`(d1I8)e};A@S-'
    '`t{=053gYB2ci#P&3<hCp$H6+uxTXB2=XRiQ++TJyy~S#Jp9Y{>~8>G4Y+}fRei%xHs1I}$wsCtg_mb1@p3EH1}uM%XYG|f2o{TP'
    'qU`B$gfA^3&<AJk5tDv<dD@wIR-Sq4to(EY9(c)}%WLJ~DV<VOSzH!OX&6dO#r2@$j!ApAJC*DK5Vs%<186s0VhH`WZ#7J|21X)F'
    '$BDB#6aG4J=5$wv_+<GB``2EFLxrua(!e;#_95rOAUuR35ih%g<5ygUm>a$Ig1>|IUh*VxZ-F!$3AKCou{Fsg8Q{&k(4Jal+RXG='
    'r_it<g}nu!=%v+(8lexnk6wxKxcd>e77R2PrkWIlpu)S%fK#~d&3=31DHIoNMGOr-RT8)@diN+H`K82eEbs1CIQoLleK-$D&R%(j'
    'QhCmKv;dOw17~goBybR+=SCuxhJy_rU^GzN9ZdJBOg!u7Ly#mLF7Gb2_PyEA^mc!xo)HI6#0k+vL)fNx6AtP0lMwLMy1jSK9vZY)'
    'Ht^XVd{Vx&!e|hHmVyh{`V*ZTV6t4p<S9jwmgc6TtrEkNS>i;1wsgkvN&~=f1_;bI$fE=(4f#0Z-*{xX``|=ZS4wRG#&c#>U{Rf`'
    'tM>F=`}vQh+Yk8^az?J`Kq##)7dA$DM`*i1$iRX4tV<v$Ckg^BHV%LFZ5>ySE{|r7Xe*)HSB)-AJbNyfHn`_MsQ`(<weUwm5%JSq'
    'M7j*%m!u9<-+3rvs{`sLGRd|WH%eQ+)72KquAnJb`op~iVCsx8ZD4%ZzX1eZU?X+zrqhQK3@c6DV|E;|-UAN1Y9-G|HIlEXMFh21'
    'hipr;)tO9(-6OfddNa}*soar)5MnQ!if3cE-7^WicHuLZwnF@5!b9}~u$}J$7}bH=+mjcH+dq^R?w59Vox5k93y%t$L-yizICc4C'
    'DsD~LlS3M|?@PV|@LbfXfat1OF{93S=IcZTS08B9;D|jvNz`a^2#-hp1*2jLg<tKt`|blAUqs7hck#6v1+ERZOQ*4z1EH4j)*M1a'
    't*b%A^mAQnsrtE&BYeJue+uj0m!94cV~bST#-5$xQvIV1O(25++livpcfdeRwt7B-6ayy~cb((01RqeoPAj1tayv;>46p%8s*9Bl'
    'F;Y&TsZD2serwk>0qCb_EU+BF)R)iQw?~HHP_;LgfxCw#v**@;({Kh>@giyBNnv-d_~Uo>=H0^jMCsX+!qyVoK{%_I?9~l>^HOQx'
    'DpWz?M_x+^yO{(^uvtP=HMZ!<L{B)xB|hZ>A=2Lo)+7y6`X&)$eb_PFMqyuVF7rDq!gT@H1vl>EyxtC?kn!U0jsa54+zM4$1O;6o'
    'D-bEv1x;5z0hp2SF%El_xY)j?hNF%BwEXiIbLGp!xx&UmuCy>y9{4_2T->$CH*@ynUXK2*pUpWlGr8jI%?OAr4Z&R?@AG)Durmda'
    '&Z%X}39~ZzReU^Adb%0`9NdI&BpbgkMeJ8Mu2%Q<fz}Q;nz`D|Ufp3bOJ-Q<wnUhVG-w7u6<HafFXS4=+E!$GE3rgKJG$i-Cgzs;'
    'Ivl%ikt2au%~cNrX>AwlP2*L4M^b9qVr{8-QdiwHSize#;4al!A1qD}Ib(UC8qV5Nd*jIwXMCWvI^diew1<8|fnn|ekYF~ZbgoVo'
    '7gl&bqu{pjw6L{9wCM^~At4Tkls$D39_@+CD8@}qmG;)~jv2eNryjswE^a-AZSKr36lYiLf$J~^=jjZfPkCU^9-Odu9^2nf*n`gs'
    'TUVGFvzKS!kdkfGB9X9`va!z962!7yy&2l-Axm7_yBA9Vn;*cNYLDm#oWw}_!jzv{{%r!NWk(Wk2$1BTN}dERq328d^hx5IL<(PK'
    'zfPu7S$Pi7|B=upz6GJQE!jhcoG>(`qG6NS0r>~E7mGmpL?QvLkhw=-JFpXrmmWvnH+L!Nx8PIxHmu;q94}kM-%-FFun=-%ApK{O'
    'x(bHG>>e6L2zm+OGKn9&EUfzu{e~COtzS_@=QWIHU{zvpXv=YgLps(1A)*6Vd(})YG-Q@#v{%ich^Y+5@emUy2IgWGe>vusszfd5'
    '<38j<-Mk#*;>O*PY#mPF?CE3=5EFCq9@-(g_2HA>@JvlXa&s-LOBx^&uHhO<z*p?unOte|IsM6>gBSQzT0I{@C8??j%LMC-><={`'
    'Ibg3k=`aUTdoMu{;Jt8A7#RD5_$qU6nBQ?w?uhF4?*_MAf7Pk@>H*S1VXkw%#yZ>=p33&)v#7o(E{_&B@8#^l=l0@ojx|3prB&zB'
    's`K4N-0|4a`0=LtsvYQPk5hm_Mf{kHOpnBG6aE3WXdXHul?6ITrLI5_7GV}*){A(qq|{kSA`@Otruh7Qv7^#n8xjZL+yik20_4UA'
    '*ut!I@4M3IkM`L8(&|L{+5|uvRocT-_S!|#YH!)A+eeC{KiP{<f6mVrA8$DqE|ni%MM;cK#seQ{zr<07w%Q-K{t8Gaxt$MA$209&'
    '_xTS{@FQi@{Xr`AG4zw^B=8ysoVaYp)=U+Bh(Quk+9R5dK4rI}x|NWXP_~*n3t~_xqImES1NVRpib{bE+JpC<`E#YGH&J>!ShGe|'
    'Xk7f#k5SaYsJnLaT*X!CO0_pw>BMQ)1-gA0EN>St95v_PW!MA#jfw`sve8Jj9Vtx|v1X+m<?~8-u`N})uvD99IBt(zD{kI$mhZr3'
    'vRB3tzOW0&9+u~RJi>*F+ne^{W8PcUb`@>`{CyxHH&T5sda%6DwO3W66cZbnwl7{)(*tpyMJb*_s2OzzLGUoEDX0{7hOm{JH(6$O'
    '=0?lcrnUS3kX~JhbX;AH>|nk6TQA;!f+on?B2;cMXa?oEd}(jCu%368mry0Wa--4=Jk?)r2K_I+6{CDv9{FXrq{Lr-W%wxsw-|y&'
    'C~Qm#2+Tc#iSrRpRL=L7dEkIWi0s#oGjq#%av9FS(#m3RZ~j&*^}l~__UtVK$8a*Uw*t`L-;&~q?CqP#)o$O6yzgAvbAH?^&)+Wn'
    'I9^!aEbcBAwr5)KPPY~LyU|E1GM?}Uxh|*y{|Xm4|286KAjGt?-DFs5p`vZ^UcNmC6g)6fvhzKI6b4j0yCEQ?Y0!YFMjMQ+o3IMV'
    '!EDW=9&P*zlSt>%Gw1RwTF<ZDcLpb&$p@v~8~+pNrkIq1_4=gJKEduT-rb@-?;$EsNGb^J@s0A}1VF)FyAD`biLMwOsJgGP11|;A'
    'T7dOI<NV>LT2GZrvm%u%9Josfy4t#wrEBT3;)D)5_KL$i=!ls5F@+IRBs$|BvHourX`rv&06N!vAgZb_mxrP?SE6Wtg9la>@+WU7'
    'fmu$W`ce^(ws^Wd*&gpnWGmb+WD_2n24Tm*ct)DWBXIneXYLD|lJ&Fp<eojg$;46FqUjWq_pwIu<gi%Z@@nNP(e`igRwjK0k1+A('
    '!$fz_@8UhDv{%3D$|il{lnWxfu7GY0GXX>YemqN8Lq|yikNS0?YmcC$Z%#7e8X%mAXA-y~J`;orZAYMSHrBEq=X2uM;3Io#J)j#G'
    'y;L{a;rvIw!vZ;}u(ic)<ah%Z55{-5E4+=AtwwxWxxoH#=$jYqi5VDkapz(VemPe*!~_2k-KZJv-;+2oWr2NEfbM!m<L&KX)vUKU'
    'j~Z8B&cD}(<LS<9y(82o3-&_`p-9bzC-*{zHJDr0yQ$S?K9x*laKLBuZEfCG<GvOarD9|ItV-twj^HMKe&Y!4;>o=u<;%l`jRkvl'
    'wzzpuY~cL0a(-0p?Q(t^KLJG@M=PePTRnxEc@N{!o$2}}nQlt}HOBp=U6F-Q^_pk{00ey`1FbUwA9}h@r_I;>`zm^PVB+F}FQ<^^'
    'c#NgU7NORSipu}Ffm~_$0kFMz<yIWsuty%27RG@SmSn+W4W|MF;3+`81*5wxWnv#sqQ!-1D<PI!JV1XdKSdMic2;|bVAP?AUxmAG'
    '>n+WPn_6E4T6k$G!1VmaOF*TkB4m7Iz@3c3u*$&7COd(u;?18<&5GA{oSCVdy)#dLz8|sIw&~Z|JNTzGGev*CyJk-<(yt3M^!d&T'
    '{h7VNg-7gjPw-c9^GRuG4}Uq=R?D}C@mFDIQMM@GF*)=tj{Z;WQ&2!hl2#9Y;g4I=bW~QaE8(06ka{~Wyn$dyIb!|k!uproF;&pu'
    '#YZph+TDBhUugJ(6u$h>l>tGp@qjab@t2zfk?mIn{RmyXB17@tO=T+-htuAz(N84Tyb^t7ETL!ezYHTNM*O$K2YM{Os-k=qhA#kl'
    '{VZjT1MrfI8oD(${)=~PQrQ~%_0h|H`qn#tQ~FG(I{4xpk0gvXvUv4Kdmx@ZnFt?k@YFhNH3hIKd|i*$7@}ZHGd0xeE6I+udPyqS'
    '>@TT1h;jQg=C{c5Onu?@=*yeUfhTlD@QFsNRx;LF^ks<tl80saz^f?4tJ@43a<M&ajK%3%><XRn(Mn>0&ik|Zq0as*Q$$lWHR)X4'
    '^+^U|xH8Cx)Bqq8{;iPl2`RoMxxO}wLm_4Vci9}8Qv!moz38|4^xFL))tQLc)D*D_TM}wS3+?F%p?%S$Fu#uCpxPeQ<5B974Or7>'
    'Vt{36s%xPs3iQcC03tD;$5>qpOZMQPZy^3KSQwTf>TOcQ!$RX4&nMSsTnt`(ue5A_^-jpA5z@PWb^>}0dwMfnj<mAouEsa9gTubh'
    'JL+bmkN%2Eg5GZY8}@W-1mOCZAR83oOgGaOtc8xBxV6M<t?4TEv;Sk2cE;$GzM)2SiAQ~=Q>KG9r9oHY?5GuC#fU6JgVJL(80rp>'
    'n~^b{7W&glB*++?{vo)^{}ouFvQeyzj5jv`WWKO9NZw4F7?WuGu{}6vZ(jb(_H}#e0q}?R)Hv{P<#RizTVK2A%swq{4Y5*PoCb6+'
    '=8R61Ru-X=pYwD2QSftqURs*^C#*5g0kiggty0-L{{StN1`WJ-1LixC;o=y(!t1CI3}!^<066<NoSLzrau5#B824GqAOmODf5~JR'
    'VB>biGiNH@49Fm1?xlcsyCrS|c-=2vE!`dPTm`TR16w3x7=ncrcy#jjoJ$+_&^13_@IuZ8nM6tiqB>^vg^DB33Y%!T(9(+LsH+p&'
    '6J9J1Jd;oM-7R~0T0RxlN9@Ii>Kk25yCpZ^U-dM29zWlkmhbulT@Aec&q4kLZ#J@6#}{$Jza(d`Jua<o<?OKqduT2Pj8!?GFKl0w'
    'uF#pT-jk<dFVfwMx((F*wHyZ2q*x1Z%m-8{&~TfC1;OC(6!Z?y?5M_3>EmI2f{blr9B~pKS*{nj>9Z=<OYtCs!eWZrE%H~ZG=-A`'
    'k!u6TB@&0OHr_n4p4<++Q#!jUloH!S8ku+XW)lIW#P%V=r@a>Y$opDsC}o(aHLFFc6fff^i15lTbO7lclXZJo_UI|4#=$o5qW^#p'
    'fqi<f{jgE`w6V1`w)QI<4IQj_=I8+A6G}ZpJq$&pm4G+R{?2(p{+u6RnA`KiH~_Pu-~r(Fb6eLLzl?4J>aS@c78RFhB*zmp24m9%'
    '_8ichm=!c<LZaWl5M-y#%p^6UYsR$A5thW*CB$ALY_d@SyC(3w1VoSp2nhn}@mlC->Ot(`e0E9gy&0w+Y}4zxw(}SuO;6g3=Zn)r'
    '9Df*X_%H7{x3=x=DP+bbx17mQduXPxbIoeBH=Z78WG{L&>0Fy~F0aA6(#|7zb3hyDeM}()s|ZYDWArQNgQF&I4SprNAF=PA<<D<v'
    'pWl|BN8LDu?TNNzXFL__>56p%$vu?~cWMhs(brL+PIUpKWEio=h_aWchh|AzsJ&uO9(bii$)cu_kO9lZF$%KQ>sHfIKViTY`?!2R'
    'kGEbk4Qa4k?PeMdSSWnF{dn|v`yuYtKObedW*Bm`X&{7}0&pHUR_zw6Q4Ag^`#z2mJq&O-O=8shc8n|6=;;_<Rqd5}6!pwrdH$8V'
    'ti@!nn*_XXUYP}S@!nSX+9F5z2R4<H4mitq?CFa~(C;pPj}7(IRW`G#>^28mhAR~3ZrUq1%6BHq*M0!hR`yfqmA8Es8DEOc4~=le'
    '!rpxG^0gQ7I(W^}hXJxy>D&$nBe!QaiHH8aw6e)T6zt_05oLDne(~ClJw0JhT(*Y>VHhBomF^9J+{HoFS6A6h9VN2MPjQR{7V7aI'
    'Klt?HPyTo8gHJyB{0|>}82jz#pZ)fef1_ydhL3E$y?hm^8cnyHziwYxv?njvW3#C3(;=6?$8gfFE$eHyQ`Kt;b(j{L2n$PlP;_PV'
    'H!H0M>kSuI!J`UhQrCy;LpK9f0kfR@4F#mqJmly=bsO_(|Bz=)P<R}@n4dossu?KWmg>!l6VB5Hg=94fR9>oJPgj9i6j=D`sy#SR'
    'T3JW&i*N{0htO1uW9Y{vA`1r2)Oa$OnqgP+7kU9N7A9XvhaJ(mp8&^hj#K7eW4Zv-R@hUa<&PvRffReQ^>waoyuu4D!!8qPLC$^U'
    '+F7a(o>#_>nYqO~#&`?e8PBEBReNbCVvM83{r!IRNuX>=Y;lfx=p3_qIcC!$uXc7d4YJjcm89_MM)S%B*h)62;8XHM5{21CP&;{i'
    'W|JbmlfX?Nt9J*ORAcDx&4>2TwZi6*I2X$Eg4FZTfE#7wriQEPj7p|e#t}1~Kmf|2436%MD0dUR;|0y>tDCbQ-w4JL@m;5v0k4=a'
    '!ir2dH_ihu8ht$gv0yqrL{p0!eO+{pND0)HPK}5`OkWf3>~yk(vZuOETW7j@GZy-`g)C>~0+5vA?aAWkuwVj_zt@}dtAy~Nlq?~f'
    '?C~?Pj!gWd@@doHs+=zM24OdANTvr6bjzc${=K{Q)Wuo~7q_NLD+|Sy8R#Xjd~00*;(A8zaU(ex?t1VxXqyBe;j8Sj2Dcc^eRfv('
    '+~Uvrz^07~08m*0e%4UokL;(c;_|f~f)~O@V86OSRsX)mx=;^F^Q3B@8&u(GRi7Q(ee2hbRXAk!P4DV}Wrnj6BPxqTMlsA%P>b63'
    ')`l~&qVrU+>q~pX_HLeI5*Yzyh}q+F8@XDFWCpA#@O!1*jpF5T`}qp6f&pgT-QtF3pF2GFB1gX+qFhG5gyU^nZw8}0t6pn!w1rY$'
    'k6pv}wpYZ(RJ~FRxZ?c_96>jPl9B!*XW!hkw>ERml^uAUE3NEez{ZKG(%QKkr=%#(UM$Yv$rT^o!2%dS@YW?gj0|+JGHMUr=W^VE'
    '_9DlBoI82v=2A`^V!QWCtFmh@oCCUvb~gNAPhE8%fIUzTX2;I8O+WzrzH^ZS-Bb7C#mX)^y>n70v<%)FDL!1yIhO~^*XLjkfLpoZ'
    '<eELViOP(fm7J(1%i}XV<n&HF-H+v+xm5urFL?R22}1$hl?4O^LtGT&ECA&P<IbgL1Tk0{y<w3NfD8D8<%qCUD1ZK`7c(=(G_p)M'
    'VW^?BC|15bLS_Y~`MIaf27ONibNQl!Iy0M!=eoyDEp`I<>#q$tT7AnlyKybO7UYIo$oME9+@+tM+xIST{wRB77*ps`$Wb@U%+Mg}'
    '$Cjr7=BOo`dTy_71AoC8sA?3D`}ksoqn{(;4CMr2aATsw9N1z6ejupbIq7lvaJS+N!+NHLOh<kA{olO{HQwtJeRzXU8^PBqg}@WT'
    'WccD8RKG%*Wc#-bu0>;lg6ZMqz6_7otOY#pR91>CHHP4oM%XA~iDt-X253*Dm62m2+XCHMhpeVnS&Y*d&^r+?UB%}kT{*?kGeXAG'
    'WQ3g8Laj7I7p?Npa>G>2{>Ic3Q$-V9889s{omRBM!Y3VP`Lj<UL`hR%J8*cw7gGX(oa>A&>VeSh?aP3Rh4t^9xzXay<<jc1LRoZx'
    'mr?$I&d+1)N<h|;Y4sjn7B(&vA8(<ag;EH;h@z)beiv;%!3kclvH=uL=U*+vDU3hV8gBU`Ms^lv_>mTRQ0YTFwxa>z@~XsbE?(QU'
    'C(aethYOo~<=JP)+p%2{kJLm2bM%3sOFce!K5c_BPqV$|fC#F={&&6$KB6C)uI?CY1mYW-8my!KKtOJMOSDQ<72dUu`mgSMeJGOP'
    'nmSf!?@*w@qso&3@Zd@<$>yZUtl)2@H6Ssbx+ez0$7m5T6HI8iI1b5D^H|E)*nV;bR6c(4-KO{Ycx3(XaP+<UKvBJd*yDjc_vFha'
    'sAx<EL?Q2b1&A<a`TcnBbRyB7jp5NAI}yilmRinPWjG<!c7iPFB{K2TRYQpCaf9`6qQIONHYc6QrPAI-d-5hn`!8Ks!mpTxXays5'
    '4qPwoYys=y++VP7TmXi}p1R|wM{;m=Rm^_u=RblV;A}0Fo;|VN_^-DtXMO=g#jjs-R;S>18Q6b{ZNzE@IQ!8Y@VjviA1A!^NxO$?'
    '#MxV%K(9EvUfA9)<;O9G4?07k#fO=c0Ds66h4%)$2nr~EJl%STf^O1e_&C$*sw-v{r=7ZB<K1Rmi0De6jLGg{{Y)dDCSwd7kKcn)'
    'u~b*r*V$Mq`E|m;1&*mqhb{h$h<q@fxVr8qIYx?)+w@Mb-H$Qe_waQ8R$51k`T0$wgW|yw&|+1?%}U7fhKQf8UfP4>29u;!t`?;>'
    'o9s-c;+zuEo0T~HSywvI;2n5_-gG2N7pbn%uGStvsxkUbWA$*b2t-c@v=;bY@5g!_8vFG-yq@m+mgaXFTRCj4(Z&J8!E%m9Z#HWX'
    '6M$DeB@;FZ5jJMfnW~;)=08Cc=^*uiQc~hyG?wrQ3WlPA(p>AAxCMnG+QeBv&9L{}G;YQa5FEwZYyCx-8PVPd>kf{zjH<x90~~B4'
    'ROihDS=hM+BopIvOiyqe4kBycBgMUbo@0)2_V4n-E^u@(GR*e4x`DwzMu{ObGh?uj<C0#Q7$y=Q$1vuUcM|9|N=BT9VSd`Va*Z-d'
    'dKQ7S7HCG^&y}b@y)*8Ks(RMnQ<W1_@)*j)6F`}%l7K9i5xFk`P>;{z*QTh&7mW7JO*m+9p1x5dv%5~8DvS*aP_HWCrE_J?zI~&x'
    'wN==;%x42$lgcQjXp*oz13y>_haVi-%^A5O@1<tzjz%rMo5EvnZj{wedj14Yc1<+Kn8WK=aWOh}A4ZA2u5OebFL1B0P;gjrQd8{X'
    'ji`m;v1nRiK}qRFma4m@i!*5FI5EtDS8-Y+)6V4)d>pt=DMS~F<0}{rYk3sT-r|!nz-c_6SD*74hp~GJu6%U!=xK=0;aFt6g>l$-'
    '&vF4WioAh(Z5SwOP<Wu@_SC9?#mWMPkfBho9N7t#+II&y_6&52n~g4sVLZ<Ko;|gM^L*1cPt^9)Lh};qYz9w&vhz+9)^EFm9z}12'
    '!ukb{Z_YDxrToQv&dN{4!F$fwdF=A7sK{H4g4g%}oX40pmKX}W3*amAjSJ<Or+BZ;YJriV^7Tu+tcal#w}DwukJhL0%-8K*r_<Q_'
    '+flSr;0`FMIEd<feW9K+-HA}MKg3=r9q+^sW_E@U$`bH48jXhf`$cwE*DHhRawc&aj*^6*GNb2oES2hfIimbh6j{oN?U+Zk=K$4s'
    'N`f$GbiC#!famB&^#w?#&1{x~f?BlE6}($SqKK}Pt207!svQY|d@Eu*<thg@vb1-}p1r|a!`^u2+!`*etnpX<<VcbtJrqyJQ)m7&'
    'feC9nlBvYu!z~P-kK2#+DM<CVawX*9#i@!0{x5R+`k0>vFJwhL5m8O`ruEG}G+jOMR1C!>c!MY4QP=uSymbXdv#_4x$Quh6=G+Z~'
    'KrQ;8PA4!KJg`<-cN#nb)Jc5{-zd80G2kfyoX6n527Qf>X}?X0&CR8UCUgK_tKy+gi_A+JE<SqHC#ME7ZvT-!b6)*$kVQhTqAMf}'
    '5)%C(Pw?13akh2DfUXl+Ow#Me0Zuy+mTgpEc{N^LG%9j}`{hk?bWD81xxra?QN02wziIX&(Yi<@u4NEsPDnv060W$BCG1)}z+=d9'
    '*6+i8(0YHX)rTYO4+Xa6fusx*t0A4rdj~9h447uJ<wz?)ns7tymzJW$%oNlYef|C8l#$2N3+}k+ho67>!6%;t1}fHD^}|)d-OVi@'
    'I8y=QVA>ec@lNFgM+2qNT{!UU=@IA2WjdiMHozC+ly@9h)Z*r<IMBw~!V?X+`i|4lc0AO4+<Ukk4>cU8<BdM*2ONFDBaXhnA$L3!'
    'u$G<x#DGSAKoJ;`gr+D;O1vWxqgO!p+p}RE$So8P#HVf|GD_Zd(lknIlJ5lREN<{QVOH2%=-?)jf_WPc-aoYyk8=Jxa6^0-#FuD)'
    '*&gxID^9x}3U=QWa))<d4Vnb#ss=M|a#J}uK8)N?^8E3kxK^9Ipu_;*^`0n)ttLNIYKI8J0=O^+4f2P3JNDQu&U5dK4;Qu;LEb2>'
    'UJd|AwbKuC6ssxtwVcV$L^PX9Bn+!L#!|GM`>V|s8IW7gE6y-@oWecxBl<%t1mbG5|3D++vhSb*@DHsp*Vmh^zC?e>hZzS0jyZ!m'
    'H8yeF>^nMU$p|k5!-j$d2>EBXS%mRKw({~(h9ZP$yws@1vI#8Hme8+zkYPzB(!}%vLcP=IS3gjq3wA>T5y)PiR+f$;(ZzS;EQBig'
    '1+5WReiCBX>{B4g8jC5eXe#E`3o7%+eREub9JzlhDq(7+qWGtJ=E`dKQK67b)F~WCy7jR0!e1<;C0Nn*vC^GP4vTJE0_zKJBC(iw'
    'ZGruvpA1%^zKMfTw>v^FCu@-YR<65u4ziGPNZ7uLTJgF2Y^DNY11hN(&+~<|E3po57AMz0)GuE82}X(ce0$f5kGJfJF~FHRbu|#<'
    'wBVFxgAy1B2?Bix;uNkQwXt##(F`&!;kHu^R--~yy2oK-Ub_9&+Eq)W+I#O9B6e6-MXlr8k&2UUX_ggRqZtGA1L8)OBO(A`W3r^_'
    '!rr2Wu0}5Ut}tX~KP%SxyJHq`0SfafbgaeZ(MupSlj(4!TBh@!QPD*ZfcDYGKwY>CG^1`+$oZ;@UMf~%GPeKiz`kkailf6ol7unV'
    'xhdxgc_`@GH`1c;B}|R?&u$t-Hw~)_d*=&(fwx9ntV%<{>9mk?^}UHnZ(5?q58E40Tj9B>;b?yZ-A>US@9=+>hM&OTTR9s^V}rMJ'
    'BbAQ4Zy0FqPgXI9Thz4b)TyY^)*mIs<RrsQ@AONf56$35%ovUJf%*}r>)vh(_(Uq1Fe`*$lQO@2DNaG-?$CFjmF}G{ji2*tUSY7G'
    'eDW#Y+L=l;#tihprp#`Iy#);8L<NM}bl^Gw5!s085qslV?YD*Uz%U|eAqL5NBR87(@eDHb$7P^~*DPmzl*e#{b$iZ*A$uExi=Y{?'
    'e-gzDkBSd3@Kn@olzULY597wLQVL>mW2i;zl827((oo)7Q768sYoQa3&@Jx!m?z1)F)V+(0hy4|$Q5@%ZAhL|cpY(VVJU^OPl&3V'
    'b=h(S>xh1MBkZh@&t2L_5GsY75(QBtl!-k;%8EMF9Hc(LYkw>g3d#U2+VDc#Y{>gnT9(KOnhu<pE{kuPy!9g0(r|OKSB)E$?<Gt6'
    'Mx~_eoS9pi-d;d@zIeT`{y@*n?aK}-Td#mu0qMLb7wthZZU-`LFTC=vEKj$bxo;IcmN>FpMBI(L3sM(s8KMq#E~YHbNa{e7v#b!k'
    '5t8A`sgKPi9v`(tJ&Q9wJ7YVr=vEWndSKq;MRY1;u|!{A?o~J9!d@zDRa)DilbS~6V-scDDx2^L%<rrSmERJwo}4?Gs>pnka3XRJ'
    'yNKx@hbOtDKq9G^D#|4z>^(<=j%bO5wNW%1wpY^TqEZ=YHNqJggHf6cLfCThXe8pVDxh0Q)YPiK5oxfRjyC$_Yt>A1K#Vy7an}*P'
    '&mC|->Z4Wd08e<&g6Vi@o)fSzk=ECbt0;*+w5V9MLIw_OMXzJFCKF%+#41R_lzdsxSDjeu0hCNCYV@YE^Xf%3e5TjDR5IlulkQ5('
    '2yHZ2Zyt>Zh5Jb&md3zHjydTrK{}qvaELaqfOIl|Kw2TQX;Uz6^+Hqh7Ti#4z!<d2hKwzu21olOV9bGO3%x{HHF9vWTxpz@RzmgF'
    'VEwu=QdiwzudS=|L{gkq+L{}Ok+*N}k)&FHy(gdg(L>2svp*yvzHtpwLih@Kg2Pc!&p{Qi>9rk0yIu{%qpqv4@%M$Qj$J^dni^dT'
    'C@)lM8J+@&p{vtIrQ0d(V+UE$@==#*lx2i#ZbC8*+YW)mV~<?3$FHbhQhq(1J$s|DeakPqc)Rz>Fr~eV$Xaha)g>x>Y7Ff=(BK07'
    'aYIIEcu$j>kdx+H3u!<(Jr0bVJ-7|0|Ic}7Y9tzs0)_oKKW~3G4sXKn93hLE#p1mQk;!*_6aPPS&3SU!RZlI?6d&C!Zk@;br037^'
    '>xD<I^ThHU0FSeLudp#{Q2W3TP@2pe-dD7UpkC6b@kcr^4!y6t;WNx4M#1-+;a@0AM@#GLc(CM<MUY2T1Y~)=JaQ3c1IKESeQ<zP'
    '##GBNOpkAX)EAmhmjETF|1hAcE+w@Xe*xSZQTjGdtS2P1F>y@taZL`RzH;V%g?@0qR^?0$_R74$(+5|n4UbifrOmB9(ca7MHk#le'
    'IW*O_VQo)e@ivNn2$@Z^V@^`Mlq894VC(d4?5ouQpg$2OXOwE!1Rj=6ngPnnXf$^K0wAjItH04W5;5?I5+XYjnUe`J49z4?p-&Q='
    'EtH82=wTv#vgeffO^6!Or8nTCpO_NGac1t7*3K0#-B;<zN<Xc!-mti}ZBLJsR?io<p395Qt<vsA!vvFOD(@emyR5QkRsDxUA@&^('
    '9($!>D_9Vdq!!GZuSA(5=~5L^K%n+hOF;gL%dA6MQa3uniyTDhsnyczmXp6%qb{_?Q18{GPzt~Q?lH|3JYw8@7#bxxTYTo*@t7OP'
    'qvG`SCFN)X3d~1Tm;rnGJf?A&duXqrW3D}MxwI=1uc)vd;;@{{qZg*O^k~01J?V^o@7#D+BbY>yuM>2As}TZqodYlmTvpSMD8xey'
    'G1(JUy~gqEFpy)ao-60iawwM^=LLkd&7uA0^PhluC@$oQ#}+q4`=}*w_>$D9I_wsRo0D7$#rDObkszOJE~)ALb%lRhDxS?IJ4C3G'
    'ST=bw9ZywT-XCW$3Etx^eR+gu8lPzduEO2|V12Di)tml0jbTOWR2by8su!1anlnT?aZ<#m4p!)k2rkc8O3&vh;FAip!&wkH2-Lb5'
    'H$9cF?-sU}O6TT4iZC?q&fEixCOtC5ro7SDg?IVDc<JdKZX7oAxgC4wvHkr7!$TnheK*|E$(*^cH6h{?i6Lq#{Qc%;R-|B76Byyv'
    'eZi^e0tPBS6t||z55|pImgn+pu#1bky~ZG+S;+86@PZ+rgK%p?4{9XqYO@gwroul|m{j@URSd=JE{9q$_`-wX5=$2W0r|!J4SRiE'
    'R?)iLuP@k-Xg%%rRoXp=*3Vk!gU<n?sQm<DEqAl;OZ0WQv-QTw<w&opwm)@5(dbo2s&W3JrlH5fc1ezVkt5wa{2@I8cZdH<HsmQ9'
    'dchuR>FQ<ssDwh5XC;fiXpgmYW%l7CYJ$>Y0A!Pw0&3rpO18ZuKNWBVt4?3*h^lUZ=vLUhXE0R0fEo7CkHrUul2?w54+dsogHDX#'
    'DTvq@at8Y~QjrHU_HvfOxc9kWZ}6{nzezu36@`BdezfmbTg^LZ9SyK0JpH|M(l-E<33j8Z+`ci=3hnD-I8gg&aO(w7U?*5mqOFI='
    '@dhTNBPPf6KgWkj&T~BG)Nv8Kt{nJ;CD^L-2^yw%^=P<36R&YTM2M^KYk}xp+YBoE&6(tY#VuhqsXBBm$ZGU*P|!MX&1z)mpq;;X'
    '6E(yGB2}AZE(Mb=XXY76jqF!c$!jkl;*TD^0GG9|KN1}E$4%XVA=qvWGnaQz*S*5#CMFmlCdqK!W9=ekDuFUsa{H=1w#O0@x<64o'
    'P$e)4r)!_UB%S&6F|r$<XX@-<$DQ!)9~P6C&}{V?h{&#c*Uw^pcXFlXia<L(>)lsn!)RWEG#R&B6%EYlGq6}z<35?Jl&?=aXYT~`'
    'T!q-UJ@4aC2<(4heT>6yfNTd~_4o*~>m}RFvqc^)s@G+3A5TFxpJ(MOI-F0giG?7tgR4zWfyJB4&io!ZxGk6N=Rt}to`2>JXl~QF'
    'z2;olb|#llEzQ1jZ$yPbp>y}FbKw!Ui&nuy6VCWBn(ni|A<=QP+6Sh8da}56jl$e5FuK|sPf8059?_0j49!1}g%B#CN7#bN>e3Eo'
    '+aNzF9yKq(&-po>-Tyg1k8d<?p1l+?*4Or?3%a+WmZuetDo~!kU0S`&PNb#$IeYacx5!^TvI#fMpD>{VCAb$}dcsBT=;1dk*4a4u'
    't@7nP%IUs?2E>#1?2%_MZR;Nz4>!HlXpv3sY~8Pe9sCSTRIYo<M^N}u3_dGtT`8?T1uU=N)z=-*WD|j>hIB0ncXqWWQtGjiS=Jrr'
    '#x#?QAlIXDVTw9+Mf!BW*qlcT<!cj|U1fc<xVb9bdlgNkv9+^}EDSQ9BxeiHcp8*G^Oz4RFfx%+$lSPWr^U@PNj59$xs<#H1}1Ir'
    'p3uhEy1GiXP5=Jy|Bh41;g1~rCnNBA=H?j!Of?lhnLP&WeEM&neEIPwAAk0de|+2>M>!H%ZTmtTVL^U#^WFA`B9+u!az<Q7;*0bS'
    'V-1i8afFB1iyzcF$8ca?<E0ptSHr7RXIE&~d`>X>SN3qx_7A$7F~h-zh)6n3Wi|U_X#2x;+A|sr8&>h*0QS;j9zo&MeCg*}>H?R9'
    'TW|s5KUKAl)p*>1CMuR{jhKdpfvUfKyx0Q0*Frk5JyZgz(hr;73cyh@afLGlK6};(3ERfA!upbPYnvmL6TP$8gN>=7m&;t9aaMi`'
    '1%x{hJ5bC^&D@CC&7p7R?(;bX+(S;BTXbUOJjaxHW@4Nx&*jCzCbe=NUG=MmC`Ox~^PZ31`i7F87%2AgXMO`%2<Baol?h-W#QA3r'
    'T~pnZR_C3$RUN=SsaA6xiI`%n?~5lwVSQm|h-B8i;T&^axx&UxfNZX?1;XF?XQli&0-8CbgM(Oj9vuM4A_v*>9eZk`u)YY0A)*Uh'
    'KhJC)4Aog*2*7JbSYU2oPDgk$M|d(k`XewU#XUh2GHk<mFi$wT2kawr)@braY@u%z_{fOK#1@#i-gJ3I+j%qcprBX#R?+C1sCfzs'
    '^tL6_7|qMA-<5&K@EKCm(N(lDro(1`fHi02Wo}=n5&n@Z27K|i|FR3+QFS|!lu+0l5>mz715Uz%+%<^{AiDU6)YANBYk$D%+atHi'
    '*A_vZDeTTW6Dz6_in{TDZx~h7K#Va$l9Q5D*nmSOdGV~W;1>2%))}b<%sK>auf{1z<@RbJu9Fy%ZDL@`*~^FfjN$c1i1JJdjRE@t'
    'f)I5Lr1gp7qq{-iUK?T=TW`Fyb`J0T)&t}v6@HyBG*N!pyjwcEiV;pmuJ9wOURSRBpzl3LDqqE`P#!PFg}oU2^^$zoOX`Pwx*;oP'
    'VXQ5{QVO|sfF?Nl$ieGZjUi&#Hb{jO+csdN8IrPe<&+8=f<|MrH_G|EJ+j6&WE=_SB?5*p_aUCGWR?gaO_t+NdD@X#3|Tyr4Ca1T'
    '6~Sz0gNa-RHM-D@ngRo!R8gD$BI7YawX$+h8{&UgpcIev`PU7X;$;<)2>$}1QugZx)hi|(e8Ex#>6iF?U=>PaztAhygliu?(MIDZ'
    'HYU#2RVCO}YMZ!QR~regs~lb9Zznj6uF`tpm@vM09kqmI2tw4C3&~*^@6eL}501eRH2wcG<8Rb9)4yWuji3Sj&xpGbG%^))gSj2S'
    '>d=NYE%*&aM42TfQn2b(EmCV|W?z8+eHrHWWjNoLWqV(W=dGFH1!+|Ayext8QO&**COXiK)2J%$j85omagYIu_fjBSO<if~Oe?4?'
    'p^a+F%wR2Mg<hqyslqCIwS@bz)z!JzkmkElby&WBtDOC!p&BO;+@Idi!R?(Y+F&1~cM0IC0(0Fy99IQSYd;RFg7i=qu#NpM+uc_c'
    'c=84zba+fiYC)j`+>5;Et;|bb$Q;CPKqnR+jXrB!0xjU!0==+#uXyPu26x~5jtLN|jx@baE!1a!3;C+#sXAHOzwxSc%M%(&1~E#c'
    'h5j|-n!<CwDs-QG;VrqZNqt}fAR`V(fStE#>u&gjs)|f=)uYh!;zR@7o8c|$7PW{80RjK?MBwfS>U8hSSSFeMnq4%Fozh>)@T5Ov'
    'oJB|#6|8}&P2w7%31>JQaC4{5ATzj)J{H4S!QZ+!7bWIj(*jzF3mj*DsW^85*)nN)q#G~mMk$;DwXpMjVSQ3JF47IBjZhf=*o^xQ'
    'LN~5qvlz71dG^F!yvcfOWfsNBBh{E((|$5WhwY49Cy$RzeLN;0YM>+<gxESu^Y<{XOMOlsnrA^yx4hTvA%!I*TC0#{q_CJQY%TH7'
    'DRY6gnvuW4a_Y9|G8(tBmfo~^L4FDWFkjD?MjpNF+N%-&5@VueuDFEURB2!wP4vu+&^dp;=A6BD9WUGXfmgiBYSupS(zTr`n-5n@'
    '&RSgwqs{7*`<T>;MhkEJpzwHgXfV;&=#>x%eT=#jjLbqFFf{*Gb&qv1Se*g}M>;YGFUxbxzQawe`-b#UCCz|7l!wOvi)h3%3gh@N'
    'YedWxcWkpk<&m9x%?MG6&Qt(_<=gIU9R=wx{;=shnJukOIFE<x{?`YeefH6ZvH$0TfBLtNVt@Gj_aC|`&qI0+01AgdNREN;ubijn'
    'xlv~{58ulVugd4kBW`(n@t(agD4&fsRU3LH0et3^zSzu-Ir^?89-!}P^T=jfao*1I9kPYMMdrMGl%syqN_-Pf^`a6a`o)*O|LC)S'
    '`tqM+Uw-h#zgQxd1EAanCQY#3whHSHxF&{a-@Js$cvY1TKmPQi&&-7INaO*{*zJi%%9n<KaGUoC8ke^0`Rg3bh@Qs>N~;5$;$1?c'
    'G;yc&VANi^QeN1gGJAP@?6Gt8Cbhv+D?Por-_-u_(FgzX*+*Y|!BfMFS~ahC+<CEmiaR@1D{yuc1Ox9e??1tj@jNh7)8n)FY#ed)'
    'j4s5OcKPb0TI+K^*o)^eX8q1(f{>Q=nA)V{nPa=r+73T}Y4Ut|_AvdQFXdnOUrVUgEqfSHY7PCGNqs7ec1hdzQXLrPl;j$nP>EDy'
    '6pJM6c`MQl_@gJ$-@=wiBrej?QVfK``j7U=OljdhGBOlK%b8hn=5As{yxFJrcjE%{rYDN`kit<*e67)Jwc!0Z(1%d76-GmV2CK6x'
    '-E%4e;0EgAeT&Q@>T<%RVPGxBu#~j#4hb)!P$qtwfA=?7(P)&V0<@{--A_YP$qRti@Yv<JTxV+$PP@g3KIem$mo#%dm9L9^M9Du|'
    '^h_pK&DPPzqi@+8PaEHH=0-qt6OZmwll;hz{;*WRp(7|BRNtHZ7+?;nU_>Yx$PrU;&yCc}&=(<wS;&If78f@KfgwJCZC=tRfmolc'
    ')D73p0jmPl>Lc#W9#H-a8K_i-jR0OUn@q!EV;lgg)W9Myw1Hpgl@tRicniBZI7RFq9Wct+hFbCkA>#n0*1KL!ufOrvtm30b80BL9'
    '0WTVK#A#I7oWxA@8KZgN4@?V+p<$_~mc~~2H_~9f(ZjFHG@)T{GZB3#ddg>B19xKCm{>F^T6xw`@_!5vtyvQ!?;Z1|MTD(~<H0}X'
    'q?&w_YBDF~W;hM&jcCe$WKG?XHwV-tr7cWdC~rNp_Ve1Wp-Tg7Z@EUi!*5}1>-n4fh#B%`ZmBKs^U!xh5}sV<B=SMSuevHCT%bnS'
    ';Lt<wLhg<1jS3qV*jxwG>y8Ye$|irE4E_i0>4!3S69z1Xia@Ev3q1PV$o$H0M+-6qZ&_y|R0@j8rv}2&L+?j82*P*+g&Xwm2i1ZJ'
    'NUb|nLPT0?@`tPpS_r)A$d3b8%ZBvG0u!W+DWX>ZKn6}xR7jCijCUB^_t8odjQqWSTHdJzWcm!6-%;NLWdMPC3B!7A=|xTi!Tk1_'
    '22}DSqrV>GDn8BN|DN~S`6YjPO7MZk6jOcaH?64Tu_Iy~#8Yj(DI|^Uy_9_~%|>>N)6JX#h;}BO^K{o?9`w8|(35ukf#(QTcAIeA'
    '5YsYJ{Baj;?nc(^wJCUma@zD{6YLB?WqGdRrovPiw5Q1n2(3Cr$}8G5-V-?4+1&;75vdGb#Nj1jti4A>bUlsQ!}gx2{2GN<0yRCf'
    '3p)n1pg&&IpfJ>m^5pOrPf#;up%+<2%h2i)0f;2fdfjR|Y8~P)o$8Aug>qRif_2zv><HA0Ha1vIkt(ajWh*RKGVLywzD|Pn$mM~v'
    'pO%L_p4Oz5ccUJv;{N~$V{CW5sRpaj8`9YeRlPXf6IRf=lPx5|ka;$H{0b`mHlCKRO{)xVLJ8k!!TZwcs=c~xh5|8W`R*~Rv65el'
    'wEGRf6%n!#FiEoyX3U}|!ll6ept@go5bDzl^&{fUjkr+*PGrMoUx(?iH{L~%jcSZGz7^EED($V)4>0n$kM<cwnxjn}TnGiLBB1v4'
    'kT_z=hmW%NjR4ueTzDWT{d7S%_H!%}Qp0d9mU{6djeYvTXCM62M<76c8T*%y{<o<?sOQqCiU+BVBZkz&;un`U@K<4d3krx|6e0z`'
    'PKg@DspazJJ^ba2=IIx=36iI3>AWZb9Dw)*0;l*@ymwL7&u<lHFVffbyXD(MqDK(O>siVqY;I5VpiMw$JRLtN0^N0`4j{lW#B}iu'
    '1#tqM^K8_f8WW(C=EW*sh}9C7CzH6Bjkopm0tWHAvXtcsf3vTgB*ZG^Om`p|RRjj|K*EfEq{I^LqFLvjh-jg-L0)#<hR3rz(D-Sy'
    '9G=5w5iOD>wt5Kb_yR4T=H#=YBt`S2S3#KRk$WWNBHQ#zXF83rMerW2f-Pzxe#66;djTf77+hYI!eI0*&2KeA8?uXtht`_)y)n3P'
    'JZM9o><kJU=F=p|OqWt=Fq#J*KJ*n50Q=6Gb9GY3Kx$T?AN`g@Lu4QRwkMYDj;AA<%wO2OVBbC4j5@g+#VwJ-8>MXK|HJ<G4c1$D'
    'H2Xj3a16ZxZ*(#voAbSIu->5Nd><kPC>UZc_DZz@8kKBMp;Cxa;T(ciP{?3nH~mmCTT);^)RH;5J@KM3ykZpNM!zrg1b&wv1E_fK'
    'hfHC(Q*p-;T_y$5hXR$^)90JsqWI{f2*OBxn}zkU!ukbRT%VweXEi2WVK2}yCB=SAlP5^-!t^32o6_*GFk&bsxz~$|B^T*+T9{9Q'
    'f~^5@^%1xg_;nYlfWekiZHzPol#-Bvf+qTT%JRUl6#w#kM;t|^mV$wrVZ`9=4Ods3f$16TV`+@t#<ojr{ABJrU}>Z>>KVFuOZ^=?'
    'O2gtY!y{R7yt*47kSZ&}W2AuZ2%Z6cqMGV$_pvehjwthPllXB|aTh}1rmmC%jt1#t{ar^&sU6aEN6NC@Z!d$5RY<<%vPk%e0x6?L'
    'Ko}V{(*MMe8?_1HUw=Q1f-8GXPSkQ!43qmAYZxdYDnWJB&Tn%zVA#LT>Lu97+We8wJ%57kYp~Pvr~;=Vqnd9er!~Ro;P3urdyaFm'
    'sOW|Oqk0jc3}+_chVPuM1@$f%TtVEpJ3l5x)>ne~NU|dbM<hQ;7g3e!$a%T7HS>RcB!?s&a!a}di39}CFnQ%HXXJ|Yl~kTmTQv?6'
    'o}h`y`=~he3|4DqztL(%HTvnFH7-(MvtoVp*6V!hX0U}Fy=gKNhVtzRyl}%~I*dt8w9IUQg?x$PjW}DS)ggQ2A@kXo6k~G|W9yyU'
    'L5_CjK6zPUWK&>VIs7rJyWo>OZKRJE0}(+E)T3r4q~&+nCp8itbeK{T@j7@)*ltU!ser}`WJx1x`a24WX+-B22+FBsN7A(6;V}Kt'
    '*WWiI^uz3^2cDT<Yli0kZhI8W$$hMOSYC(gx%(JRd0b|E!wZ1Me$3<qi;+V<fA3t*URli9V>fcehu3nzY(>l(jSWpl``>2`8^-Aq'
    '!#H5LCt^MsV`Cr)CNkf|g+5*jal{FzF(o#NmaX2C3Jxi8o5%UHb{;tsxA|=1NZb}5_^c5zjM$jZ(0F#{ij~^J{D>^F$G8;vh>C~G'
    'i|R{Qft+mrmaw1I8&bO=l^U;3v4XX^FIK6Dj;K*NSy|@YWA5N8&s!OzijobozUj1Su6j1CehC8OS@u?+GD~=q48s|DOD#6K9neV$'
    'QnKPs<cdS<<SZz>rNbItu)Uso#;v6WB%%5q7cg*un)Dy|W+>VgDh11^McXyH;_pq(8VyRtvpq3Uihc`zm)dNRDfv_k07PY29$B(E'
    'z}Jf`IF4Ciy=COFWgP}YHHE#1m@DN#4Se1#1E__QI=F!Oazg<sO)V1vN%1VR2J0+pjAsHpvo3-&By^=ak|&k70Jd{*Usqhipo3)p'
    ')(MI6tB29%@MfB_g)_aezE-HVNR^1@UsKCKlaU{%H|mO<d`37b25P*>#{jL4cK5LDx%<NFfILkw9vm4i*n@NS=4CHO{bG6+K1L%R'
    'gQRRgy1~AXs~q?PwDP}&tQ2Bzcv`RzljzFSm2toI%K*pPf@YaW`c|07B8Q@h13`rw_^@fN3&fd9UGS;EtHC-w{ell>0<aLnsH=!F'
    'Bs*8ZrHb>YR>?P?7wVelr}J->X66G|3nihXluo9-F3|%1Kw)c*dK}Cou%3#P4D*WlyK@<As#~QKooX7q{rk;-%-N%>5%|HX+8l4P'
    '94~E-wX<}SC~_mY;gnrx6aVmZq^dCb5O+v6MWd_k0}nvHRu4QN@!Eah0m{<+AozrDfnjV*8p_}R*i{;aGnRMeR$1d+*xantmrFP+'
    'jxcyW91M^(<$5dpo;u)9XqJVFb<u+Rz?yeq*X6d-Y9Ob3oGaCt1`p5In)Jf*au|^|NI@LY<rP;)>?w|VVqZx)9Lly;kxBOD^pl_W'
    'Gg(Py6&j*y7Bk|cMfwqvG1TT!v-E7K08BOp<LSVY55>D8Drty#5iv$;(kSv)-%pCIB)hs}L=$rFb}+zDaq^XSli7N<JJFVmBMV@Z'
    '?E0D<POu{bDEz_Kt9%;{@kJ3C7V2BE+ActwL}DE-Ue$1`A`o0PY30!&3LmOCM(&P{BS-DQ`!s~vi+GVVGUVKtW|P<H()1F(og~B3'
    ';=PMNVL}??nap~xt&nZ?@nwNa*p=~>Q=rqK8woDy!pm3++!NsdRD{_F!!Q!uen1o0^gieksS9w)=S}8;Q*)&Yoc*?%+>%PAS3yYg'
    'ZC&5p+I}qr4azi5akcIhxWq~~^}bPPO7covWu>-EfQW|rJr391nM6k-lQD%A6(*xM)x(lNgGFME{>m>0xD0xJ8<>2NFb#f_fd;en'
    '<@~5~;Rk2-sXet?*ju2mecwAb=8WvJI5OhhMKl)Nq8-UZN~8k}0%;|tTjL;kR%oIvBaiCyq?3#~sTg&X78t>xx1qtM#wNH|7s-Mz'
    'IJD7vdf%_6J4AD@>6MdN7U^*78siZkyjgE~MOtWbD1qy+#CufL<-VzS14eOx&h~BoMJ98A(VJ^`%xYxUF>Oxo=@u_9-5(mK7=0er'
    'JBvVXsQ%FVf8A<~9&NDRj=ZW-9CYzjCS$JVP1Bf0RbfnXs03*+6Hj9H)f&RX-`a2{R@jD4xDrxeZi@Q0$uY-ORCta_f@k#thEOan'
    'JSuM9b>?qsq(<xAFno`7#5<FzGqF@x8*eR)zMDyjFnNh=jEAa^Sie|yTa{;(c_;ek{4}POe6+xE{Llbpd($3yCdYSCh4te=g6K;%'
    '@a#x&<e9TNaisWoqV#kX16-5$hCPQ@;%tCRaJYm4@HQ`%2Cl*<;Rfe--sJF=v$ve(Ma_-+yK!Ie)#dWJox=K3Q3kWdpxc_;2nM1Q'
    'J}~2+)(aaqxgxtec`7<Hx7cfp19@Yz9vCjUPO{4phq%N5n$s7#qKv&ai$OAFFz({qO(=tr`N&O$2V+dt(CkryOyK|U4?AmV%N2=m'
    '1RWPEIG|eSZ^rROhOY8u1r%YcWb7;bk}R3VFq`I$de!jAnoAq*wzJy^eQhH92Syf`m`xE&IH~0C$v8^aT@yEXM{lp+<ttu}(TiSG'
    '7|plN7d*|4@hvTM(m?-XK%+T5qd9%|T*V;Za&PmEmlnpfFZ%lX$1#wpXdPq5ML+!f%MU*J#3#M#4K)Xa7t^9(%CNm4PY~?bE7}>|'
    'MZ4nZ5$DNej#+H4Zj>H-<HY*^5v_3I0f1p~b5+K8XL|-TM(#Tv>N(S$I38*~9zqoPz{QZ`p@!ojyvU=EdKyalf&im0(P;E|s6xtM'
    '8QN05?mF=w$R@=*5)?8X=xMw?8`gn@8hvq#Q$0pf75H_82u_SH_R2U&ZLA{Zm`K$CR|RMyfHAo)fOuNiK|_x6@YtX81E0n-U$=Li'
    'PGj1StvkhA1NQt3n?8j0jT-GS?Enb*RGn2(MFjXqo|%06R%=EQ?_Am6N3U-6!Q%GsGY7IflN>M%pH)C)1JvM;t@E07G@|caj@Au_'
    '6g&NwFYkHnU%2hsprJJ!7$w{T0Ij&l&q1rpG)x4V)zhz%qlU_B!96GoqWzaz8g&U~+<Mn~tFcCerjc@53OMMjK2KBqoRwmnM_Ae$'
    'F5X;zp{bXKpV(^`ElzWR(;s|J0U#*(wKVy5R;Qf#1pyP@59`<Mu?2hVKEUb_5PZyvO^Iq{h!Xwe!92RAnik&}>U5dGS5H|_m-NCU'
    'MMBV<&exEww1mKTV=LQCq=7{P2`Lfw4N7=A9c$7y-)jV=%WLIp)4-9zany8_fK%970%$vzc7dA&n84d#9d3H_&0kSgkmbAf#WA#L'
    'dNydmOKM_t!WmmCU%KXXL8G+7z_>%X{Z0tqf(Ax<=QxS9z!)74Cf^Us#mIw(S$(Qki8iIZIT$HS0UmjtflYFBWUWnm@<MU@2f!Jw'
    '${;~BB!*n;2R;G^29OcK!YI50qYK7TCSn7;rhz#X!c>y?oB_9^jR%KYaE$3{kP84?@$%CCU<=uI&kDOg=@`#oLo>L^C3|Xy!zg&N'
    '&FB#If<h3|H-K3p9!W4rv=cd=KyV}h-VVLM{CpEXN#wfaH0=+v#tw)xfC<^8=^Sr4B@t=uR3%{w&=|m*sjF{ba4c)hRX?CR&^U&2'
    'HeC?bhSY>JyT>LnOt1tZ5y67i(K3m7d##^HKMDobp=wl8tsOUr>Z_q^fVB0%RREfP^%WlvoawoIeeSETSe9WSvp%HO>dm0FQc>F#'
    'p+#4liIu->kFdcr><#G|Ch|++b>;G?v^{)H*paJ-gi|K@if|K0xPjv6hP^w$i$~l&5Wy3PjGwt}$FNMU01(lW0aUG{k!5wYYxIOP'
    '8n=+y7@mTtvS@1HO`SHrq4J{Xtw?PcRuyYpmYTqV1buia+!rvQ{s@pmI0VtqkKoFs%#l%G`TdZG?9>bEw}JUAYz_&Wq!r$WF9T`T'
    '4C{|Jg<5sALzQ{QcqW*B<JvCXNUUCB1W_C-4q*G7!Y`P-i<WPhXsKjqd*T)b;(2uU2pdzdqXKq&_bgt7)PVfTi?=%!Z%dr&O0_34'
    'x@W!ELdU;2<Y@-p7@Nr3hzI`ynh!)qqXmhJCO6j9*jO_y6TU<snGt{(VA7xClHkL3&jB9>^hfWzf8Vv|lK=xvP&7@!e9^*dTb%eZ'
    'spPQ%IRInHqqIia1OgxsU_dEWqDY*`ifF>>GF|z$m|wy=jS$%MHeeGOEHX|i0UpS8dEptxU3qw`G<Fswqw?*m6e1;$64T~5rAr)z'
    '|2IXgqfv_yxU{ms-kUC|6DbyMKLoC|G`#~*E}z3#NET)~4_WMfhz;*vz;3BwkYMcPX&zS-6jKjJ^@lfp!vlcQ5^n`rP~2)wOzLl-'
    '(c|d=HdQhYD#{yjE{(0i^t?Nk0!B3DrL1JZA=?zj*)1rhZNlCjwP$Awo0sgt<-$5_?}vqrd3$1@^$^S=2PC;A3O`yS0hAfLYlCwT'
    'Z$)Vn$nG^CN?GDPq0!dgyrnb8zyN``VZDVb@60??8A*Wu<Cjc_oD1I-x8^B_#selXFP#oVWP2jSVtfE}A)SUAEau%S4L<-z(;0c-'
    'Tv=8_LysM<<I8gNWUIJ!(|-OV*9)kD0?^DY4#<TupFL_t;!iaY%R2$>q#=}wn2>xqD5z~m^Y?XC84ggfW(e?NR+_umrN53?Ot3f{'
    '9x%>|4Ctp>VSS5^CL(R~d-nVc%G7|(bNAIrZ7<$K{q4Qg^1zI<J>`;t((019O9X5qkE`O3GsPdj!_KCr%R}FxM)<-K_a}2+?*hRq'
    'ZeOvdo=f^Oj<$tcOF!r5_&p2=73Xw;z@z^;KTqylTN>je$e@o4+HYUtwgl3&UooIQxO<OBVYj#Z@pyY{06WG^2!@LH5oyn-u(zt>'
    '%G5MuIAd?4!8rS_#-+(7m`!{86)|ayA+VBjqgG!)XZ;v1%a1-RL<!v48e|m?a9u&WL6D`I*f$Y=Z+JrW$Jn^vOje)kj1|{W8KERc'
    'H{qEC2qY{zi?pIII`)o`fCVO>8Z9c}H2{(ddN5*4^1C<BsL#HrEJ=uY-yX7W-gJ4#cW|fYi<|fOROf1*ZLS~q7=&SI2RUMj^9g$t'
    'rQX4T(#pC$JyM(=axPvacua6eeW@zIx@?gFxP+m7v>n(IWE^R7Fi4H*lZfOOAj=|b%vu}~h;PF^y}lZf?b1OPf;?4$aD*_xI(oYg'
    '%o2uT>ij}s>luTSR}-n;+@sRYiYxjM5oEZs2<`8lNoRR@7&u8@5Y8Zn`)lJSs|p34>|HBt&T?H2S*HSbG7ZHe<n}{_7haU5xIZkF'
    '2Td~KKU@AGV(FAxwd{<K0ug`Xzwm@Y&U@!ObeUi@*2aVPhahNA-LZGi^X7Dq6b_$+K`a^LwL`$UF;Li<<jD2*$f89mHvp4!Z3Uwl'
    'QD-vL>tJ!~nkClVB-N6Q^iyj3;v>{;UEi1_UfOpGP<h|0Ezn?{=xQhPPO}HY6%6o|-U7rs<LRDc8=PThVxmt?Dr(T%^o1nTM<_40'
    'HACVG4j0A*Jk^x$zrxUEpJd=7*jIBL5$g!OHCxa^jwP@(-rVyz0?qIO8bE^Z1`-6n1zbf!95g!O2LtTrf@g>^M&77aJRCICcaH(<'
    'EzfW70N`VjKOT0FDJ7@rF`)G-71YK#LEBm~jsqO$*0T>m%z%B^Y#~{p7ksFB=GT9uF90OS0Av2m#m6p1<qARcR0-lf3R+rND(_4)'
    '?bMk`MgR0+qkK7c-vYX1@pXwkw!ju0NTjA#MFN-E_0rx&U}%^p#WRtJtl+z;VUMO<X?-}!ymMnNfV@^9vF@xR)fDVz@mH%~=U{OD'
    'CD#|53F?3QV%vj*;+U&pvDLZBXM#O+&3STJ=dARlR>>?2TN7fbskKT=ZGSiZ>XuqsTQo*2dm4k2+KboGj)L5@)UnFU1X}3~j=(^V'
    'pmo*O7F*$nn;)5ImNktI2;y^_&h52wej4u;A78e&^P1>{=Dcv?oRpqA>MYbhu-qK3pn^U~6BRYmW+RHz3+&?EpgO*|o>~SJOe4mn'
    '30dPO6Z$}2?M@OBE?p*AeE|anabe{rj^)DPf{)nC(;~_`Ew)E&V%D4lh9GEAbCs_KQEe&2{=xgO%PS8_>>L_W`RX%xgT(UgS+Rdi'
    '<U(1BjNf>4UW7a%T-2tn+T&LkJC$BEaHj|@;>r)D)vZ!~u(XPXMR;|@JmAD-6w$Sz1zx`e=WdnORw|Oeu(DFmXzHRYr~}O#gAJ|d'
    '@9#J#n$Uf$ZopB5hE{BYMP_8^^vID$ZY`7_jF%TSR7mOK;62L>uni<m52{T~Vau@fsg1*y9zYx3{ye(a^j0I^ODVrVBwlnuRk}A&'
    '*tmt1U}XWY5^p4cB+wll5Vg|kglJTc!YvwY6pixbrE}p@`QcSjT947~DP6kvU1{`3)xW1Ve6xAa9(l+|fox6-@$G4jZ&Z%hQx~BG'
    'V!@7>o2aTs0_UplU`?oia|MiN)W@s3{4^c-6VB-OfQ>K=B;#`rp(Ka8=a+IwT10WJU2Kz#K)G215%#g8!^i~+<GSN#(8`Lf(_JK#'
    '!2$SHsKM&%kC=III^*3?NhMK<wI@5#1{5<5a+Bepej2QRknEn8g-1?D3a@L@0S)V8sM4S}nNGk2jUpa<BqP@gZ&V}M7!C7~6|zE<'
    'hEUImV@A|bM-^13uG@P%yAAVY^b*hZW)iV@wk?@FMgb$#F)R#1PbE^_csC`?H!1oV(iA*e=<r}y0@ozdjZz{40nnAc)~oA(;BjPA'
    ')@DM;Gb0IU<c&fqQz3QpfhVQ;=I@SK6i8^FPADc{U@z#Jg{~9dM2ucMuVgnQ>V6|a48WA3vv2h15I!?3cM<~R3ao&C@wTs#S8`px'
    '!vU}r$&-#DE-;VfasJDMgTe0gfedh7@mO6Vj&fTcz{%g9b)V6<<s&uB4^pX*)9mO&ewhLCn?8NrFBqsgkgyz6)*R9N?{d!Al6`S3'
    'hZKM0hg@m$IS{&B@!^fqPqR5>euu8*ilaZ-i%)aPRWHXT2^jK)^FDdOi^fzW$fi&JwV~<-A%Km51(3r>AJD5Qf@=W3TBP45GCj$T'
    'Bzp;D54J3u7x^g2weZzIE1wBI_{!MZuGI(YYBf)NcO4{utm0RqMQ@guBb(4dz0i9jZ5Smk6Y&rN2un?9srmX>jF#W*PyAIWQocW+'
    'wa-0;bWAT67F_!D_<JGXP*Sl@U$AU%=2@@8iw1PyINowBM}re8YXhn&e77FE5DuR%s{?pRnR@qk0ykVWTeoy(^RBt4MrEfqberfV'
    'G7f(v>GmXsQ*&tR_H7UPSf`z@CIaiCpBJ^|F8N&|b6D{fs0+g!fvIF?62!VN&EMdMBkty6e<8eroIbCZW40P9tdEtiO^f_4CO<7w'
    '7AuY$*mG%10Gvzg6E6D6$De-uB?jQtd_s*xHO&?<*_xN9k+r57ja2lo1bPY^-$y;4jrXIG*rwTfr?CMi5Bu0MvMO;ydeR#zD||<V'
    'f$a38y?7p-j8``7=POEeP<l3qcheG{Y$9nLZTuBm#^UWgse(`&w0RFsAgMY(LRv#F<o@u%?>~$E{(~<+{v4p!6g?`}`G3XJ=;OgQ'
    'jX+e|F>JIj1qxhg>tw3yL_9@~Y8bP&n-PVd84cJ9@^>G6`Q=By*OKlKg4XL<moQLRy`FZ7Uub!k)C-w-iC*ZLmqZ(G+NF9eXK1~i'
    'e~C)!37EJoEdvvmFjFuY%{i0zK|qT3H8s55k2W-OYgDDMcMfXtYkinwKiFg9wY{}jTG=%lBX=~kSlzs<vfRnnqA~GWm}E<y_ww`$'
    '|0{%BdpnQ0hF_*74+q{9^@tSgoS<{$Zd%5mjJefZ6Fe!h4Ic&HSIr_{&v_d1$_z)NZuV1l>cPz?9Xd#<AhdJmcJL+|_S)2Q=fXA`'
    '9!tLk&-Ao|H(0(i&*92buEtWigN8w=5Ulxe066PCdDQS8=guZBG(FZb*4VfU2+gNO7%_gGIMdAXgS~T`;5MopJER79Q->Fx0I+8@'
    'Zx3@R-iTVx+=JrHC6S_Rdg7pf^?3dCroT>iohI=(>jrp3I)z*m{uQwjeWE^ehM&cScSZ48j7#rlUl+An>^OA8Bw_MB_dw-4;{<{V'
    'NX=HCK7dLZ`XGHH8vmfdBVWg`Dyw|^yy%HKFl@hr0MYMT$2X%E*A@2;Kj$x5&W-ax{4h)kxiGsL3qx8D`mFQQNkf>gjeVvl9eS-m'
    '223w-T7^J}Xvh>WHsZWY)+2D7qQ~C?g#tlcjH&97YUtKrYRqnfh4LEOG<c!Fsbs%Src&9O259h3Rn||4n6)%%a9n1ShNN7}=yZn6'
    '6${7mrGas0?Ww)-gv@RS;rPaw>zntE*dz1shl4VYk4j!u1lS|fJIySUL&8au)IdStRPyAhM7HNkEcpo*3>F2vQCev{IUY>TzIc`8'
    'TdhCC^NL5=O!2zZ%t{xQ(7+1>UeDy91ZANb=P3yX<W)hCLpsef8Vrf}PfF!jtE2AzREkE#+Fm$L>ie{b_Sd5^<@l(73F`I1)r-;Z'
    '>+P#o3RrneI3!`h4)gZNt@5=+7d`@w#J!J?hUox`!s(d}Yx)Qc9W7mJP55R{TrOUCWVt*&Er5=}A_kR@dLpIqloW@WZ<H^~ktk(~'
    '{rpD`PoVTMM_3)hBXHv?$ZIoY+gGoTw3c-X0V<2A6<b@FZin45sdrZ$R9_GMi$?3!IO<6ReSk@XSI^VU0WnXrx64;=GB3m2k3^|}'
    'p+&0Fnf7G14M<xhT4N7BD{NiiyA^KFFi#RpOVFYo_Aj0%;-IE}M+^vP<Rw?T!{rf~1$Wnzae(s~PpyiG5+>IIOod>^0!9|u$G^kg'
    '`CjEb7pBP|8uIn#?mW4802x4+e`hX_c4xPS?cpH`i-di{HiofkqerqK#0bs)TwjJ)X_xB(3>vF&{xX^VydtRd5k>lIu$~<sbpy8$'
    '(=YCq$tXiu;P`pGpxy$QytkqjU-uQauaxt*bS_RHr%VEZq|f8+*7s(=p8cCEMD)9$eJg9$4P5UOeD7^brsF+Y0unKqGsN(p(a4^g'
    'mdbs?u=<iZ_JtbZpHRO#gZ<6F0<2IjP_D&3r&Sk*#}8CiYlbx}lz$xiGyH@Nx7INw1naq}F}ACA=9fyt4|qeN)$o;Jm641Q#hJOq'
    '+e*1ua&;d@ydAa5`Dr1Ru=kc8gGJd`0!ERZQ6$|-_js9yDBE+I;eV(gb5i2TuQ4UfBoy-L3W?!z`)z7$G!k*X9gm900MbPr4u(a('
    'OlI#KuLceeWwFGpi9-*brsz@2&QH)sVdCm$;l$E0IY3Nr#=l<=R?ivYr5rd~K6;dnp3Is0N7(-M)DpU93GWdMIzZ<gSr$(%SwD|1'
    'TJMl2zV9ALC2xnNX~`x|8eUuQ>~Y!(zyI#B7L_EPT&CnAu??hZpw+L8mwj<O8QSywLMq|4^z(rl{*Z1Gz^tgzWU&5UUVBigUabLu'
    '4B8zK&=@I#`ecK5y%+RGau+$c&lhHdU6Ro!v7z5mD_P%X4yC2}Xk%-CG#agE$6L=pF_u6iRCAAv-F@!du)b2_dv->jD)Il#z<yvp'
    '3Rz`R*0NdBi_Mm?mvLLo90{BB_ZM9|k4N=D3g|Pjc%6(P1EvvxP%1ite|ZnKvb8FD{yW!}<1eT+bXQLj!f{eruV8l8mHc0C;X)o`'
    'T7v|dcPSncsDBhBaO(LHsfHXv!l=<hXMUlyvS#mGu0#wE=DW&?P+C3YPp{hQBek*}kDY#n5GQ|K3IlSdf>3=Qj=_t5DmcmCBtb!s'
    'Tf!8c;x9=}<O2Q`X~-|LdVeL7aj>NcRr8k-E1<Sy^<DH*JII7TF~QR8o;z3Zf-3KUP)+^(f;+{S1U4XJ6j`DR>t%L@+7a4%)V>r4'
    '*$_1^#xH=<UKqYCUD2hL6H!Z#Ql|2GqACf4uHX@Sgqc}g0`VQAhT%ii9rLc$^wwWvdDMi=WujFgg`74|5x;|GTT_3=gi%$@K2aR5'
    't;r%wZ#3&;`!8kuOp!H6OGw7j_IKmlGn*3}i17SR1S10w-Jl3z#4UMk9Rq(=SRX~#qo=o6!vu}%quIQ~x0m`_S%)kR#B&5A*5>cA'
    '6$ZRD24G@*Th!7+RT;)`G9V7HP^QU=Z!O$w6cy!0!sZuV6)kEP&!E1q%3xFu+5Y+(`{J;2Rib%`kWC|3VCl;b#_?WGg(4ie!k55e'
    'IVg0T^c=y9{(3ZJ-JSXBt9rnm02b#&CK3OdSnt&-q5YY9;EXMy{mi2UV;$Rpa*<8~2%iPaG6>$^+iSaZUR3~R3wo7mhq_AI4b^3!'
    '4W&{=B+~8K)5)GwVKNCsZ7Fohe{R-Cerfz6n7NXm%f>l#5AX~jA%|gEBPk>Xi`x~>l3Poh)t4KCNs9KoYQA4b4W|~SnI1z8^G>|-'
    '<=}sGorsybJ-u9u*-WcJ6Mh|!TDrY1JDP~#38YF@N$8<~Fb9&FRi$Xv8q^bldP4}Yt!a*3nsQJt5;hZG9+jywIjKlaB#W}2FX0t!'
    '&R!qMaY~jPXJN^4S{9W?iqalM5P>z27#Fz`DRG2CN3D5HsGOwY6M{0x=+oC^jU4Z5RZ-Q%2Ltf1GWC6g#h71IS2J^LW>#?5*ptzb'
    'rx$_6M9DnexJq<ZUsHM4RB8ADMzg074d$`)Yhbk4CCiz+sl2k#cXjFxXW6TiH;ijlTuq^=P|?cS1fA9KbE6@y9dg|Z*TuEA9{I4a'
    'Ib^S#Gn#zGmgX{>4kctsXHO-&MGhc2K#6Dxrq2o8BHd>aZnpHw>7^G3(ExK~O)i9PCs2JwYwL{tkxMI!T!|AhiUpkfu6%6~<J8E0'
    'd=&JZsNuoK&U#)7YMA#_rl2P?@oYBJ)zy=Y{gTcgUIV3uT75darcVLh9NHB0;D&|W+2W<2fI#L-(@W^&KDC^4<|Z%#0Q@TL;Y}0%'
    'Di2OTIeIs}RKAp_a%1O#gP~t{@`b$>`ns|Jd?)@w3PCmM)fi56WzF6t0hcN7a-Mv5^}$=;KbHpo60!Z4TS@P{R&_}&|LohbuP?N+'
    '-bBx0O6-WF^YK}ckJ>z#4eV@<L|&1-kI^o0wD!$S_^mME2&UEC5@esZ4cY_+jJbx;{uh?G9ZpED`arK(iErd{DliV;OPoXReHs~$'
    '$303Y4rGNPvB?msq_wIdvl!3%?pX}pV8oJCG~*wlh97S**JVibm1zc>2UcagjFwEs>mTBHDxxTi4jz*00)EC`-9Ew&Zb#HMVQV?<'
    'pi`$D-zoo%`v=0Xy3!}fJD+oQG8ia<9YMmEn6G%@QSspgenlDa2Jbub=ftxqrBI4HUPRT2U#1m>JZnn(yvq&Q#o&GA%1#3%*^+p3'
    '2xMF6xxc=!Go&+On23WD@LXDT#s_Nb0XGHnaWb?*y*+idus(T&W3(P|M)!a_uV+=KNH8%mjO1hJ8hn?7sU)`=m<{}%&*5G0YB!gh'
    '(Oq4%V9(7Xy2=Q-3@u_E>sPoz=hm?E<h~KDAK4*csp|`b=*qMpXSff2O^pq2H}*%g??9c+rH|BAyj*fS43u>ZE7Q3(d;P5aIG-!d'
    '4cLQ^?5Xu!ap$7=Wl!$e<D2-E$tL_lV^k)0Y>!Ge3xT&;dN&NjoN%WVezd}6*1ow>T3IH~*^y_$)wXiOuk8NBq_WHM-sBA-aO<iw'
    'eQ8luB4Kt!!R&~F8LCcd9Rwmh?;Ll}P$%3Whyxr^UBvvGyzg9Dc5QZLQmNAF5?e`oJaihPhM7q4Nn%);R5?kIfL1+7jE1x*I$ZPR'
    '^*zQ2{8}Gv3pX{w!BxDrgW7<J%POR5dH#k_@6t%|T7@)VFytfu`r9My&3&Y_vI#Wk2;(cecEgJ5skl^6pMCyW?4LjX?DO9T`@S)o'
    'RT>z#pD#sUw?}53D{Il$-I%7Wz+BXmBO#YzzLU#+g^b726;E6L`rGL1JmwrmLtUGakM#|nlJ?X^Kquj#4!i~iD7>;}-?-rVtn)8l'
    '&C<?lOJVC->EcXl4*plznQMV#3vjU2Uz9)vQ(`aywQkMPZ}<^?oxl1jak^KYrI-s(oQ*g6gLL3ibt`N2Jm}@4ca=?sYV?oxXrBFx'
    'z7OqF(t+%^W|~%!i0pfEcNn{o)d(K+*L93<3X&(-+qwH{^WcSRwQhJd^?k&@&E14ihM?WYH<0}{w&1w3t!Mo-Mgs%ZQRhhmdyfRZ'
    '^_}xgjVJ#9^1i$&uIt+Oe?CQ5yYiqdgaF6&60V8MH&O1(dy4(ac5ZwNQC%%)ByJFpZm{D3RgESFG0D~hLIV;=vLyytiiAK&Rrx4('
    'pYG-_e}%V(efHVY=>b}@?|mv;g6?znxb~X%TE9i1iUz_=pw$hYJef#+83+5RGpSpJ3f0uT-@<nlvG+maN8cY-hydN;TjU7C{7d%5'
    ')%-oMU|*gVKlvX}0xAf;Okw<CEHEZ}`+DPA_ZII+Gp##_yabK=nxJ6=Qdyv>`u6>dU*XB`V=}%17TK=jd>ON=UYdwX*z3;;fl7oN'
    'QFinLp}K9I^Y2CZZp<1{Y{XkWBQqH}d(w!aE+dgdiFop6zulc5N3$>}t4T^K4GWZ1e?X!FBZaePFdv+9K2W9J%n<C+JbC~#+o%Pr'
    '8ymAo+go1XzClf~(2|sUNrQeA#9pKo^z;Q};ye?$)1l;8HJ^aZwCJi_izwsl16Ipm`SRQ2Mo4EF-3S1~VC@S_E4=pFxrf6}S7GRu'
    ')jRPKpM_q9RsuA)`{1g3r#fJ#=urdr8Jkch$oUFtX-gFlJJ?h5_R}7u!!pkFd^^)}mf(J(5vmBLe&EKSP#)20BG}F)<5DHCCjuLT'
    'b?9MXd!U`68X2*Mq2~Hu@<j`Q>)hM$W}gi+fy87|01r22xuETlBEj2oSELBoQ)&xmzC{>ryEACn(aC%E2w$V26I?t9d~>UFLO1u('
    '+NLDJp^qkN&wyT#;V9m<TMSMCC&o}Suo&;^9kgr6TVAPg#Bbq+4l@N(AylO!(A;4&EDawmC9c8184eB`sk1=~B=wQ75j4Sax|wNs'
    'Zd2*42;RzPwgr#i6=H67?b;zDIZhOR#nKCy&!+gB5~(=v6ZyCBq*oQGscK+NG}8S#^>jy`HF<|a?kz_xFR|k4DsxOBQYuw(j(#S#'
    'FS|L3Rnb5<ofsoT%?X3Qa+YzjqmQ|)!qN<b2DQW64?scO%}flVp18S%<`w=AzYX&j_+f5lJi9eM%rO>NphkzT67HXC{{m!tw6}eT'
    '96*Noq^z|YgW4W@i`_s`cd$z120z<Vk)I$?TJ5_;w6suLij>n(X81ZnG6KT#^R~>w5b6M^(1erhP*+ixR2B^jD_2o=NGS6aS^A(Y'
    'MV>2mCkG(|8WjugAE|q7Eluz=X35bjeM-U{kEVU3TwWbozeDD3BL8--4j))P5#HifZXWreOy=dsyz65!+uP>fO${bYX(%+Q3_KIy'
    '(W_VB6LBGP_HkkKVlHzJK4A!HKl{s&04mTyxCH0`URnH<sqHAIj8ooU=M4$<5b$ym8#X)veCWeJI9YE~Q=-9KOf@V66eJGudwCj^'
    ';^?8MuTY~A8LcVej#l3rR^P&iFJ)O{)kD|Ub_2t#I7aT;mbbQG%n{C=I>1Bf0Nuw$Yn<b(M1<10`P|hRZN9a+@yoT^oWtt6;tm{Z'
    'Fo_qddvK!5`e^p~T|71GPsj~8gFla66=2o`#tyFpF4}(J*2zwcAPO@m#3O20fYYu}oAT{T4a<)Zu7+6}5A!&NcQdD8#fRxqX=a!q'
    '<M+Sc;T+%IalCHgRSR1zSYKyLl`J?aq<2q=HTdi^{uZ^v$!kTo8iQgLu7vwcQRI<#P-K{FC$M5PJFGAYcI*NoU!2|<F8t$6?$!$!'
    '_Hgd;eec}VaBc!<ZUJ4f7{P{?ERg-Pm0_wcML#h#27fjQL#LO&p3d$p!cXRI>DY$BxPB9Pr<!Fk)O{5216y~8g8oLZf*$RSOvHGH'
    'Dq>B#1Aq*|ph$hHH^6Vrh<Jp>O#a#?Y$+=5vUPG~dSkQMof+P3ghirni*0SEpIq+{SeTs5)edHE?q;?NFE1ED{^sGLjCoKjp+yN4'
    'BjQ2yRUhn45BB#{#=K0R57Tg2%T=02lbk^Ggep5+$ee}`gu)v$NTDX&ZA*As#z3lnajonq@LbgDe9Z*!+z5Qn$ag20En{45EzM<M'
    'ZRtQIvVFlGXwkR})$^@(O(FY!Rmf-G<!`P8ISMR_zM3+)zRrJ5l0fgv#YeIC%^=oy8b{!(-P@x;cEe11vGQq#_95k{z^ON=b_)Nr'
    'ziqOKV&uSm{ABc?okq0O_pteVp-WBj!f<;OoF6o=(n4W<B>>H#NF%)T1p|a*`$0v%afT_vR)w1>TpJDGxoJ!dpz4*X28+SDAem33'
    '66vH9aic97ZLe#1`~4XH;6mQeT4)&T?*t7}Z{ml{B(?BE=6uNpG+bByOfx6>BdX4&zNV6UGnQyVQK!F7m7AIyiv_`zEJBGTqNh)1'
    'H^;JDuXe9JJ=)pshb@R$27v<3B|wMyjEevVm^u$AreMDonUYo`C3wbwO)*P~8JCrWxRIjzv~*M1)>I#9Hc%L-q&uuTf$G}{%8ZiO'
    'dpubetZ6KAFlhEbq{(4!52OZr(U`!N`K7k*mTq|1-Aa2{>a%Pos34}aU1~w9m>8`Un}2Z$^U9Hd&rM_U#m&{i`fBYE&x?ZHgDOb3'
    'ABdu{1YqV5z0B+$HN!^@K+;;uzFNzz-l6vW5g-G3HJ^X9&Bvbyh#6T*X~?Mi#?+^WPgBONw|=E+>=nJ$QY_3W;7ac_L<G7e6`)4L'
    'X6y+YD>{zr=ji~RRX4kBl&erk**=mSy;NSP91f>fs)&WOR0gLDYY2}OH-_}9H|tCx<VTbnI`MXp?h&koy#?j)0B!H%L|RUcP=qu1'
    'gDlAzTpWU5Ryc&+T6<_iPEOHmbBYq{R8@YV^NoPgr<vF-N{3S|s?QJc)aeLkcg{r=tg=;5PWH@2Om3nzT!f=2!{I7y-rN*QNtLfC'
    '><%6Pb&6*h!eHFQ7RqkI*^bG6xUKM=buy!0g0)4S2Slmg{<^TX<*hB{&d#9cPvJQyyVEBV105&ge@T9c80iS0e>^!5?@#r0CDRC9'
    'm*|PhhfW~Ilj1OuKw?%iW;_O%*_cDgzL3Jp(})J?J4T+NV^sMF9<<oZzLEV@FGcN;IbI-*%^5J6y_Xr0N-fHRBY-=*EebE6=Pylp'
    '&(^#br&O#xli(3~kK-D&N!NnV;(aKI{pe-p@g8Lh?<HY%yyY=b8j&n<@8(v1DI@&t5?}8cMpFu#ood?_MyCo-?g)i@;~d}O@=$mf'
    'yYtg{5TLo-;zKb;^vdymV2r@p0`~|cQyVR-zS$Q*h`m5Ietk3f`(Epti6Ul`cf840umZUobK({ek?#?Qt%A~5axg*ZD{Xx2{qsLk'
    'N?*yR$}4?k_ER7E^WhKv&xeOU`NV?SmXDS!eWi|}NP}kSE5`))Q2I(AW{J{QL5-R8iN3*}&Nv9wj-;ERxXS;ib#N+SMM{-m&>yrR'
    'mxsjfzoLQglfrr3o1GT$wW1Q<xIj@QoCRM<WY7io<mT@6+nh08>Q*W0O+N<dh;h$kKJ&Xl?2lhp8;&SO6Y?cG1@U9;2a1#4y)&~r'
    '|BZ~6MvTp~<G?&}=fA1-)}DG>tNHtn;UL$3f9|VVw$@<^g{jl@0asbYa6xp>YjD4>r=UTx#c-B7pqWR4ogpL$PHW9>ZWr#3@&e>f'
    'Jy!;9AnM!au{ffBn*&SV;vG7czT-T$|AbijVJP~0M9}|LDFl5NJCfk4q3D<@OS922s{`1mB5RgFZ8QM#TsV!0>eJNNSdOF4xy~iF'
    'R)<Gm%v5sZb1h0V(5Ekjhy7`<W0;-_hp=@~g7rXa22hWLL-Njn0x=I-^6tt8D8NxiJ@6VoB4wLyS*C%%L<;xrVif-MrR?@oWR-Il'
    'II0Mv|IV*OjvV=8M24z@vf*<_Dr&uUV|V^+?$QQ998c%AmkLiGtL@>{`Q*y(#rXh+LFqbb5>ks*q73CvyyHYtWF+lBg}gI4knF&3'
    'pgsOMPW<LrH_|{HG~f8~M1Q)@nZ_QBral79B$WoGNqi-GkE8vahHpC(JspFf5WpvcRQA1>{qyc{80193$Po5H{eUN{3^Q8ITV-`e'
    'M3Ic!4}45{LeM6$DeCjgUdYdE@V<M`R<b*beqNGu_s74#aO?XEOWyS{`9R3^v-B+6!+|r5fc7FtLo`#12W0IZ@4bU}NT~U~_YR4w'
    'i+uFnYr@Z|`fV_tsJmh|&j4hw_vlpUX{ZT8*n_9bC19Ya*F(hE@}3D<j_P$1GmX$BX#9{Tk3*~~HLKgRA8KYY`b{$k&)niT=s)@S'
    '9YjtjM!I(2PSUj+EsspqgK<csS+1hQp-q<5=5QK~?baJq9BXkRvW3z%ksv&!M-Hjaw)u@1k$G=La#x=2UV0_VTJhZx=c`4cjc{nA'
    '(MDdLXibqc&7;Tg*`vpX!OF&;hUwSN+In?hvAxUsVonKN($76$xf`j)A)rSJT?mV#`AaLJDpjo*<1cw$th{fZ^?waFG@|elRVUnP'
    'gk|G4;Um6##kicsaroLL=ZmZy^nA4Q05l_(`KMhnxdzTvYf^?OR;`H_IdsW*O|&J0kK~f^ik4K3P%q*eGMSahB#Zak?2q$9tq_HQ'
    'BRW<u<NlB@rc!kWId`$+q_@+qvD>InHYsd|4aQvN9-CIt0GKK0pz68<NorgaBPdO-QEibS))sA7@I}~4JtVDoi=lBWB0Y9v7SZQ`'
    '6a*V0f@^H}pYRcEXH!EXowA_b3d|__nczyGYeYYT?>{rSl*yqw4;*d4zx*&4bqivzV-GM2n7FINJ#bF6mM2<UaY|W~z(6142^0>L'
    '9kAoK_{52}e-$t_T)pQ{4#02XhCo?0PFBLb5cJ{?G<@p{>y({FvRajvZjB2UyXwl8XmoGd7Cv50MGNfP<HsqsC92V&F<f-z>mQZ0'
    'CW4mr7u&R#+PNL^FcNmB5^<@HNVeCCexlyarf6xfhqI61=zv1cC01EmDSn3beM6=&vx|?Iw!nH}Qe$sD@tw1eJ~PK9J&Nqi5p`i}'
    'vs+V)Z(1e*qTi)dj$EDLshT^3AJS20%L-2AJ3=uy`;_vh5W^3>2v^q|8X7=sDrXOE@=H$;{FLVFjm{D>>r_#WYN-^_&`~&L4n^SF'
    'gDXQmy@7pY9+bO#s_<$96=_=k3O7w7fY7z6^}d>_En|;r&1wVY8EUrAP_Z2;cQ<~5omi4PA;p$lR2&xV%3ii)2_s9!uaI-!ms>J`'
    '5*DiPk7v2n+bA6AOmLxE?ykrN+9x5DcfDb*#lWjR&k&tsJ!QmvDyJohy6S902KosUqwMA~*0s~D=2grJh*~C}zahA!mp0yeM>GZ7'
    'b8bGWGPj~8U-e`{WhjnCX%<x*ED~sl14+SPL;y_-ZHN%ta~Ge}&8@b?6;d`I2N9d0w<Okf^R6z1!Z$@_T<Vc1MdlP}O-A=nQ+@LY'
    'Tas^v(gjW{oo#`9uASv^ym}wMf(oz?$BLn>zK&V3L|zIqVXlBRd9yHe+Pj7a!jja1j3(y#8T>(}4x=iJJyw20ZZD;AnS$2|<{rv?'
    '5F(feAol*xfe&$Pf~`7LgFlsM+wJF7t@}!SyW}`6VytV;7{)qpbK%vE{PrqrGwgl-@&%~|DJ{P?pJ-7--8sD4QjLaM=^@Rz9Gu<d'
    '#vD-nSlB*jSma9ktLNUW5=-Y?HZ9;t1{bNqP75*8gNragPF{LrkPo?szRxVGgZ@5q-C0aFn!EKpH@zmUanwxCuc$rCupxDfyn`I;'
    '#LhGk@(|K@w8mb3V^PDMRb7!f@vQz71(=X(xeQx<*Zz_>@2Evgby0PK_I}EE+NkQQcRv@a+7-RF9=usBc?9Ys-oR?=&sP57LkuJ#'
    '98tV{^2V3(bj{rK#^=j{dpVPRv7vU#Kr52(*;I&m$pi$ChTP<mH#?~=5C>cs<H81Md$=P4v0U_KPZct!6=9<tzHna0J^+nw)Pzf`'
    '9H!Rn{-O3<qYSUJAYugEW5DQuW*oNlh-kO$kb?a=^DL5M^j+9`Fh!jHIt&?E%jBMn7IWO0t*SYL&ED(^;tFTBxwxdP`6FWuetdC_'
    'gOm6LAbt$9B?@F+2)(8U=~|1!7l{vHwA1Vzuy{q74@n-vI-zVbx&p}ESjcX^4D<S7l8I|8C}$FPVl+x|)$hL69pEt{emb1L<%Po9'
    'OK*AX5GSaDYKeN_<+0q-IDDPmn(=O(%io$RuVu#F%I{nOZB=a?cjF(U`TOTVwNd)HCPO{Q?V4ex5Btw+udaHSh}CpOvPVFQ?OwlK'
    'Sic0kZJ0mW9h=UddOp0nnBkw8F^Mk=udZPkmDLsTI}=7lA-`qFUn8CjBcvvT{gk<vn*o`6qkQMJ+1e~F7002;2sC7!XP<gIkG!v^'
    '14OiQ^B_9yb^1PY7v=YAFeHJI?U1T*EHlubNDXuY0e2-%cK3W4@9ATB8gfi)agZ%x*9n-WH~C<9@jDI-1Pk1|a241;R_<Pey-_~('
    '9sx_Ag-<!?P`){=#uTaniNo@dhPU&NrVCHj`P<qMk!4oNt{T+3I*DiBiG)Ooo|ZEWvJQ97`B@C;kD%fcQ;1#d&*hLh#j?=X1va$b'
    'Gr~GuzW`@(_Y$7S?DHjrZld9cky?Xd(vqO-(a3>P-6DPIQw?hyy$y>S-p!l&@4g}qK0(zfrw}yCKa<FQ*KP!9{qVdWln(?850^0_'
    '>E(e5!06<9LIc2#P?c2H6;;Vt7|r51SP_9~_*o{y>ga+`dDa2xDz9Sj{M~KW6LOODgrk0YRlyk4<0GXq!hCIC_tu{i)0i<B5k_`w'
    '+{eTXVp+_uoNe!XE#|klLEPwD++W5j<5N6-nfIN2kwP;hauZx?V*x0E3E+@T6TJPrxA|*Nax*LZn%sdyyoj1@U`b<b4We!x-E>LV'
    'V;NS*I?Tw?Voc-9U{MZBq8%30f!Jv55ZOmHiptD{J(Gs7VEJer%KkfFg;TEVyt*1#+;M3ww|)`Gi#HqrImvE^owL{VkVXUcy0XmT'
    '0ht2JFa^W+8Ph0mn=M&})t*!E0om!vbezRnh;-YVQSK6b8Xo{oN)|a25S|cgW7zWyoCPb<QEG~SY%)3nd4M!H`t(qK{Aq4|`jC)}'
    '8u#L~Ijt9l{>!c2!O+3+%V;rX?nx%DCeU;5!V6&B@Rr;ICDxkvp_GxPwMIso78s#o3F!eY0md>Bk$ma*AN=tTNB$%J!I2{${rT|k'
    'EL30-q2awa1uM0iS@6y;gYF6nDz2b1mcZAkZ-9eui2YxD2rV);4Z|atZ)%PaUy4Ya1E<JTa@WFuf*Ftumbs&65bY7%5o7JqaMd5s'
    '7pa<IA2wxE?f`BbV`F<~M+<A4jOk<SBoL)=5)m!tR+pW=D61cqH)?u;N-^2z=V3{Ie6fJ}MP!_Df`PFH?&wC5`KTtf{1a2zxG-nl'
    'FCk!8^$VUa6^)zdI1z>#<*3JEqej7Y^$x_--N(TcEe^GKwDUmS`z;jy6MO#`!|)FllYoD~O2dD=*$w(<<qCf|=AAuFKW)7dKjj`S'
    '(G$zxh}Y<T#f~n4Ik|&ywZT>s%7|B-Yjw(^g}h6g2v9eq_1)I;PWwnqEqBHonD^6}#pJY|gU19U^><5>Jh)>Jk-3R!_C^1YIfW|K'
    '#5BJsvTW%5i&v(`a<|4*jvSnV;uIPdhWpQF>cr&Tpgff->Hcg3bS({DtS4$<9IuQ&1sCeWxFt8vfK0|RlA`L|m0S4GZyMhIDZ)$T'
    'dV_{T5NGbR6m_qP@jsDQb$*5C=FNq+h!yyRvW4WNj7Vn8sI^e2IV_8O|9p{fR6%sW|8lFhbJtgLTXVpB3+t-}Bh*|4bc4Xg?63g&'
    'x%v{!l3`Kyhzb!7OVms8dv<$<KgutjFKSAa(%sy8ax}=smt~pMk=E9#L&ZP>b-Gq#{$e|k?8VJ^iRM`)uL)aoJ2m{=Jgapq5{vAB'
    'W+`%A0$X>{zi_FunrY0fu>*Xn{Z07W)XB2BAoFQ!IhvXkH(na_15UoGWfot&c@c$+(R9{A)z;c;M7w1T#@e|O+3hGUDVWmFZ>Lbq'
    'PA$9Lh|5g3M0&c@Wu&;Xl1Y^}^V>J^Z0{-2{p9hJ$gGutv3hbc(VI9<nh8d({s&G%3A0QOBt$}+^`iLdcipLE$3S07eNzy7Q!Ijh'
    't{aO1(!YAYM1qyh)iNTli+o_jg4_i~kP?Ia17NNlS&4X?Vt#?QCXFqoa!qsh{Rd*)Rjb!fd~Q4EB90Y~cwbFLTniYXh_I45(Gqf)'
    'xRBkm5Gc>Z9j&OlNHhdJV=+c5!OWlF$)j|2zk2;sU=C)GV0p{fpGc>1=a2OboQRxAAfi#CXHbDF)7q-uZK`XDgEW1=e61){x3mWQ'
    '9CZ#_6>%?I+JSucl=DsQK&<mOTWAVByqn$p?#CD8<XxOVC2$E#pSYH^Tw*MHC7eW6dMSL0*wbq0FITi6f>SidBD>db@Y5Lihaj?y'
    'q>WexXlG_#5Pm%oC{gVpjG(;o#P@zoNZpr!o<&kQQ6D9VhcP;&!&rSJl%0u>)tqqHI&RlzLi8JAvo}{Ug?}iR$4CUGkLn!8QjgaT'
    'QGX+T9t2^aqnupB*z1a021A`!m0riCrsZWLjKO*%wG^Hj$&Fu*)WU|>1{?QIEFupBCOy)-(!nl*7q*tY*$J8W9f$<f*tR$^4^0|9'
    '+>?zz(KzGVkkbXpi<Dzf6>^#D`;`#5|FAjnzb8|D@xD|%d9t5mXr0fb$W%w@@;drW1`?2EMPmn<*NA^%u^9ZBQhcI1{I9bcomv9='
    'loeLvA|1s1%AS;IXqj)=w$WPxNFaatLiWWAwKA`~{lZK)E@^vz$-8l$DJ}P6)*D~8(Q%;{7;d_QA}(y~vVpz<47I{#@WFOrp`6%l'
    '{e5XL>_m<Vu5;<h=p$%I^!F!wJL|flwL{R@NNwGa-yBAj#zJ7atRWs~mBXku0&9rCsvW}aeJC{diLmxdL)-{fAvOZ~eCazm9#8(s'
    'I_V6-Y2yHOs`!vXtg$+CdFnyG8;Q^XD6gEv`m^1!X)J6=SN=BJfZ+<qAEQ-0o$zKS`7y?<stcKCtgOpNLuX=m368lXZAn#<(8U96'
    '>gJpPFQ>0W0-W?uRMe};Q)BD^j2ISRww&K)UY>EjFYcdCs&<^NpZP41g2W^^Vvi#HFbzd1OabyY3ff+Fdoqf=p8QxmNvm1b_>m12'
    'Nh~YDiu^MTgr52m&oVdK5(aBW&$UJ2^hVnw-Ra1OeZ9#RO3|Y+hb}gw_S4&Jo%CxU{l%aZ;mL}z5i6}fRq4U!>kZx<3g7nO>4=?`'
    'O?Z}DgLFT<9^w;56v3el=y|dZoKV^5bE&m)X#zecdsqOK<ivbrAKC5g?9L?~!67hq^Q+HAarOM8ZRUo;t4bqLPi&x){1U8AhR?dX'
    '4mw#=gmZILev^HEQznT>6{JQH)fP%&qfE7xnm}>n!Ez`hlo({TKk3Wrj$kSi`3MCAa&Q)g7*zR7@j++{Bbl4L6Bac(LKv4LF;@hh'
    '>8vc+o_Awd#{m@D{C6+$E?cUF8|PTf5SGKj8K2+IWVbbaQe)a!bns?w@tVk|T7RBfzbO43dr+jDx+Jn)U#SrWfo!_1o#OwcNj*0O'
    'tBKg57w=0p%mtLBg@S3jk|A0{;1prqEw2EV$(M-~IqDfN53Mc!%NN*u&=>a1pS9J(aAB+{O?lM#Q*I*Te+HA;KBN5dV7@lc=whbH'
    'UyhV$ljE>21;RuE=w8@tSl+Vmv9&d}x1sTt8i=;nN0iS<hS;26+dp4Z3|6(R<=w`1Qa@t~Oqps^iV9I=gw=~Q=47X!2VC8nAoM)k'
    '=B!r;G*+*?Qrr%$*T*m3Y_~jK>L7K99Dk@KfYd?a-n)~#F=w7hI`{G^(alMC`$RgR0l8891zAt(h;-XXZ^Re+&f)pG&f`lwI&XZw'
    '@a!>|#%irmaZ|)>!RJ%RLsXSdq*(=}M37`@b>w8ygD2~d29W^bL=w2Y`YOgEZ%3M%Yt-qO4Qb{k-=3vAQptWe&1h&Qj-~7L6*y=%'
    'VdmF)qqE+#HIAc>yxaJT2-RlAKr#M?!p)f{llex5iFxYOQTzr>tiRQn1AiMVw0G+x%?*vQs#l=Ud=M-T#H=t>nW{J`gjYMHQV%LX'
    '<5=THTeg;l#;y@R?`@)N6{!nd4^9D-o4JoZ)UAu?J%iqi<x%>pz|ImIG^DIPH5BnjKCXThITW!s1(O2sm0y&bYuM%X6bod37Uwi$'
    'VV=?PQ8%a(J5uq~=yoYr<*Sdx9fzor9$>H3{o!Md>I7P<=4u^+!`af%)J1kGE^hcHRz@_e#k#3eC1jTgSN7p+1{1s7!&g~pN7wOV'
    'n}f#wMvtGqpT$oaDx5Sz>j25FD>)Jvouw{?Fo?#^aVS>>v=xei?O*xs?LW}$5<Or3of8(jdy)}xzQ%T7)ArS}xWR;WJ1}g+5BTw;'
    'l<Jh9<Eg|KEvRe;2;H46aEb>=0|mwG_?N9HvP&)eK=LGLg$Y<7FzLH``Vs@|1f(laXh~jtdYgA69~_6CyE`K6<;^W(!XdINVqh4i'
    'x$y|&p-;W=uRl}WsDyS!#flviD|YZ;8xQ&qovlN1_#^F670lS2tei-^r|)<JB~rkdfgWJ5l>sO~&=<XHH+HwL;?2tCvE1TA<<JX~'
    'h!$R5z%o0}pKuc*cxZN#YdGVPyK*Zk;{-An;lBu0I@;c1V0TPcI@v#fy3FSE?!qjuj5mALo4O2iFJ7Vdk0sJXGVnOZ=g=mhW5n8M'
    '@tC>a+C8oqjNFo!-{xo5aljMz3QJR@V(?ux{mA3uAJ0wu&jf>GKO(}JL%*nJ{qcj(;vXOW{gK1J{qzqXeHj1vqd$H4yHBu|23;^j'
    'au2z>U&ml76$}{OQ)bDtPHI4BNRp&4E}TN%Hg~45vzA-TWOp7*$VTt%qPKMkbBizD51L4K^ra9Yz3$h){&jr>{)%BUb)?>;>&5tq'
    '?WSW?Qg71Rn8m+Wb^?n*%r7VzqEB04>R@!V7z$o%Yis22XNQ0Lr%w;#&w2!4GTq<To5lhD^@}9f0m;9@KnCh~AzJlSs>fr|3jf!u'
    'r>|A{Sr<;l>0QVKBKErc!-qruBw?>c4h`uu9D#i^J?-T;57af}&r||N>kIVJzkYJ~<A1joD2Rj5<1<tW7f~915A`MouxwOc;_|k;'
    'YtE#TpQAmP?%*mn#a|HZ!aIb!!@6_0ACgv1$auMUZG7qu?p~ZPtgp9&RtcStgm^pMuH*2X@f$C!-t{g_DvcXk!G;(gDcxr4NhUDH'
    'OCi|g-+&c$48(ikXcDd&JtMb4A2>#u_yPZBmA#`w4R61HbOa-nj9ySiHT>?QPd_+v#M(r)MSCe6<5gd1JJ{4-V-FBTN)K__L|2lc'
    'W8w${pRO~Ti&+LOvniz&fqDk3WK>@;@l}|ku?o7grUnBQ*3Wq3=Tz3Vo0&iWDrS5;Yv!8x1M#m*Wu`M`AKQxrbSkT>qJ7*HP=4Gd'
    'dz->zAs2&c#a6uy`wd`m0gsjgs#jZzxvHxL@4crmdiR;o1c7`p{Lx@PT_r!mPnpR@VTUkxgtnIVtd39jkeukqp<HGgjI<$Tz9TXF'
    'V6xW0s3n-#8o-elz9I#<OQV4ITAd`|WJN0P7PT0z+=7llTj9j-)*-d+5w;Q3i+l%Z6e}h{DPaK!!8cpE@yi?{Q6At6zpvmEu_4S_'
    '8K!ag>FU{?ne5igyNx!`kvc>Sd`bpI1s&o_XgHDo0Y4oK6Yh$n;TBz&eY=?jbo`)##VV&Pkn7z$&q{HA`)krEDT|LP$TK(RXc=#A'
    '#v2u&v!FcMclpULQr!bd;7SQ#P9YcipC^tFl4R|q2dK6a@&ft+IKjVu@ZpDte;5Dv5B}{>hvR?#=wndn*tIT-7!n~ks~{>Zhurk*'
    ';|s3)1#rs!rTpVbbh|$qLp>sJLMScATynBEne0r*)7>Y#dlD(c-V{BdF2UcW278imR6IL1eF9kF$wcZ)pT0}z6aLu?>8+wkYn})U'
    'ydf$Oi4@o8a+l{hku*260tSf5C1iZD)^<w?=2cU~v0!O+-h~~EU6`B6gQOxN^rPBgXMTKf&G!%KLqHC3Em{)}pEWhXw9m-MVJE=d'
    'f@2W~v<z!yUD{y3>bvkeNY9`U@x(NK#P?g-&D-9K(};Ar`K?3qp;g7QC3rBp2Q=)F?g0aOr0ri|rbl}@>N-FfL2enm%HlY*W+-)r'
    '&%Ep2x<EQbaanoA>7}-ociUrS$|_A)r`5`C&kP0qkI1s0xKb`s3&s&=Ok7(t7mr9uQBzalz-agJ=Si#`Zpv3MN>J{QuEY|NxhXo1'
    'Vh%<~rn^g|hel#7eyi~3!mAx`@)j2t%uS7Dx0lIVaE%J9Vn&;(k?|^}QnHSSFAk0kcK3k3EWh&YcDJSzv_RK`VgcSWZSRATrj{}{'
    'UtKaeN4)0L7PuJ~*xR^XSigkzTdmoL*e1nHd#WeqPsbg%)qN6<Q%^G4U)OB6PyW`|xB}y#=y<d1n03Mvr7V3Bn@PoIcgLn-J;H*{'
    'N(?^CXru9wHl!Sb?+Pzp68g?P-UHYSU}$xwWsY78hVa^*pU!XHQ;%x$ZyiZksKfLR5l}i}48hU0<CqmTPU98$@}t6&m6pg*a>PbA'
    't!!8Hl@hKg;RIV=fP;kjQ`^&WYt;Rsu$ot@9VGBQ$V#d)W>sU#GlargoRAJM-cX)e@s^`XQ@$b}m=<k~?NXUnK5c3Tb-pIZx)fZf'
    'B+FC{k(i&N;yaN2s+4J2v8>K2w;1l<tuOAp#h1(R-clY<P_c1euG_u15shq1$|~Za;Kjn`T^=quQ-k~~Sw*)qF_6G=?NR@eDA4tY'
    'S=bIfj7CQSH>`Z!?T<CSwri;RywPsQ)%+W9K%VUDO!maX+>jx|t;b6)cnH*)WV*F^|9(kz^In5rGOT&jTJ;5RxWt(GvSX}9k3G>z'
    'iK8VF-Q+j2)oQ&XN5XWXr{`Fr<1ek{lcb%`O%Vw^9LnwL96Z@i5gWls>m$$(>`6S4?&$7rB@?&E0^%zsw-?!Cvf0uFQyqaj%8>jN'
    'G%;%`$yy)z<kOE2fB0{o{x7B6V3AqB6=u@Br7?e%6v(PLgP;K-?{*>Tep6XW$JCt*I$}0eh0$VZUmqK9VaQ&=T8&Q7P_A{2V}IUv'
    '*{$%p8?<F{yw~k^uTwo(wu_RZ{p4cDN}IcFyy(JVJ_OB;NPl@b255C1M8%nzOE+>`NiKuPE=lgzP=A&tjJgcG0C<t=8#1=BQdDoR'
    'Of3IwjF{qU@Mi55bu1OxE&@=99AE6`sg@n1aZmeDAmGsE=IgR%_97hjArxa;r$uXw>$1zyAmiEO7EZ}T6kf@wJ~6WhxnuN>BIGqS'
    'IxR=vjz->&yxWLKKu3GUMo6t*H86^3^%Af|YW1?ddpTNtQ1;m!WubqqY4tGdIJ3gkU@wwE@H7~2*bJCnO0}m@i@+X(YiK_ki@+FH'
    ')hz;b<6lRlLP+!bvj|Kwte!<sE;+k(W1+Na$b1#*>)O-=6qY|G!I~W9!S}DPs~fcseO;aUYt+~E(dzoTy@GE<UzgnqeO(*Ie+zwG'
    'U^D()^>vjd{y)~&)n%xfzFwZt{MqR1#n<2s-!CX*`I+eJ8uxtD`ubjk<35C9OzX6$zOKtIM}tCN_my?V+S*;1%bgPF{)$SftL%;m'
    'gsSXsMGnHBh^UHAIglXMjvV>p?&y`=i`h`LLy>zrx{yC}x3IL5zj!k@K1Xfw*woUeoJmrJa;&l*X}MKc9mCjP4OWTH8w>?mX`TGl'
    'QC$@$V0_PI?-gxcvc3wHxP2;c!GSvo(j7&&7DuGc(={<J#iW(E7A1Mq`4|u?+>wlp(O!=g>6+J!`_v0{RotfzuBl<b@RKbyWk%KL'
    'Oin#T-t2|^%!WKjtlR>d6FrPjA=h<p^1g9~qD8dpuI`>5MJST&7wuW*tXw^dsVc2Fc&KSsYJH~?Uznz&(wc5AHq{eD1_iaXskyOI'
    '+fnPbJj2TzX?>`vH4>shhPJi`)5@B_-ps4wgFBf_9jA~+yxpx+4?xId7<X2NF%gEfm|z1%2-U?}iUD!J%Y^nLxCI`@&|0zfKoNDe'
    'y+r(N1)*%k4xv0xEttr8as=H+qT9BXrpCthk%oqbXwjzKOQ><&wqQSTUOY?ZYi~uoxvAXh3Mhi!_Li=fAkd)y0fy@=tz}<b#{kgf'
    'Gx<4D78i_8@A*><(OWpf_<_Yo#}r#+KxHW@{Qln56HSLLtvQy|AdP+1qgIOOGNAygXCHdAo8HYCZ}XI_f+5tJr4<#KG*D{<-n(Co'
    'Fi=B`FYFKj<h#bpqM)4$@?W%&VaSOnC4xkC34fl9bYI7_ZyhoQ6?iZby<f&VK+HQG7+ulG=jjNy9MXnS9wH6xw6!x1l#qM2#0yTG'
    'OVWuJ1AR|OI-`3{o`=dhr=0r<WL$;vZ7;Gdq|-ghv?|Cmr*pzCcOgfK2gU!TO@FSvr3|^K9;ae*)NC7BV@S8v6b(O9o?U4?xm);Z'
    'd=zA`Tu=@BxwgHOP&N|u%abQAS)w9NmmyL78!KNLtM^lgc=j!Dvh1rhZ+RL-4dV<mXhU}MYXlsgn=Y&qKA4YwUby{$W3U;RDe6s+'
    'X16ZFf6;@#5x+!5WGaP!=B~ZUP2bk;=<0(@xvuWNF?dp9aG(!=iw~p*dj-p_3tq6~kxxqX0r?egD|o?b%YApq2<2st(0ny?yyk;n'
    'l+j7pyH+RcWbzv`7Wsg=QU}xt9#BW(OMJCG4PwT2B05J7$A@TuJ*bf(r9GhJWsSy|sG`O_HOi-fo@B5<=|pe$0J8hR-p`Y%?yl}+'
    'XPh7D=t*>+EJ`!VSBUJ0IXaGp>APgQBhlm6g(|mQYk#mMeSgdCrEGc%P)r@21YeOmN)l3Wl8l$thW?g0D27J-Jn1Vv_SnoP0<}mi'
    'MUev=HgdDgkhL$_we3nRe>*}aT*Y}7pI)hsN=_z>2m^*9`iC>`COqQ-OoR+0?Y-B-EBU8<CM+4_UM3dO>YJQcz?}X%*oXKr_Q=H-'
    '{U=g*7gd_6IRtEFZSPg!?rjG96u^6%TFFjPhAQ%_lngB;+*%^J&9kV)4JoaXB+V^;i}j1vN4>QhSi56%Dt~X|5d5BfemB2;t}yBQ'
    'i7^w?5Pk<NK@~Dir`cXgq`{akKFwXa7KvJ~ml_An3^a(sy{`%r-=WD3YQp)8yAQ6~6;yoxo-Pc_UT{OowM1AajtuDy@`$+dH-Uk2'
    'f^)F8Zn<}$m}J`9VwaL(Es^}a?cMohd)X@=qR`a{%yw)GT{4UJ*<2F%ZtS6=3OAuUWAs+7bqJlluXC2_m$#H=ATrmNALSQsih0Q4'
    '7ngkpN{Ens%}o_H@-z+YYd|z<0p~b%S=Mj$Zd&u!7~_EuS#ZF?xevHF>)zJE#e;(luIdN{%emyuK4aq~e{&VfcNu00Ixq%%dT2`o'
    'z>NQueT8m3s4L+W1mp(7+7%sJX}CZjo?VU>Y7HyDYZ4BFN4t8u=`K7T{%xpGE@nFbt%bK(jENhizZhch+;{nJM<x14e)}dCOTGBi'
    'o4q4QTaQX9)hfH3urRORFlX$Gq7{S*z%g|ONZ*j`BC76mv_;$aKv#^aI|xr{Q5L~dW}sy}o$Nt3zP@^-ojQ}l>J{~_vdz+z93`1k'
    'a~TKRpT|7nXNm@~2}(ILLK{vGba&WL*<8P@J`$FX>V^y~PxG#F-l<(fts1C-)`FCT4fKImeb_*PF4l%+!3GXFF{Ckkx87asNrd(W'
    'q3WI82&3JXZs<=8oM?dG5bCln?Dxd6H2zx`kK;PT<1vBNj2|{B?GS{7-#!cl-O@NDS`;6nx>F3S0WTe*K|{@DG}JZN+Y#?f_74y='
    'r{6-2b-(gOH?aJ^{$y_*4H4c%d?476zRvF6<E_!bfv$tU!q|><q)YeW9!vE8{RI3!*hlc1aGYSwTz;3rfr%A&#X@>&uAryCpXly|'
    'b9@ZQsF$YW>_a%7e-G>aVc)>-vFOTS^e-C@XlLfXN#DC?r}H~&xyxhO?Wg${Tc9_YQE=6uF9i)p;-D#fMEN>kDRuyre|`eq5VkYH'
    '6ZVQKAyf|K#kEp?#o+Dy+Lg#~M6D(Phu)kT-#j`NOI%_?3u?B%QDZN(LDv#%*P^k~kf4=7e=0#~;)+wvC9>-w)N5adZH)1ly*tVS'
    'NLgVr1A_;hv(~Xk_Xa0g9*SsJH%urXXS(FG_Ad4l0)3hYpVdyIv3SXAgZ&Ka#03A`FREmT!KjL2C9?#Mno9PkY@|m=0ua_gp1mCT'
    '-=BQ+p^YKgckDmm@K*`s5;YUG)p7y+7Qf9+y!6I5yt(n*<4ZWus1qm4&!$^opc}Dx1f<9JkoF|^6mV^&C7{Yy^FwCt`-NLSZq7!s'
    'o0E*61S1!aD>QQIKJw3JFr$YE3PU4fd-R9Qg_?sA@i2X!ySkp=TIW~5-Yz^sbSU;4ftD5~9HV%X94a2^+Y#u93u|*tqr$yAyJM3-'
    'WJdp(Nd2X=?~7jeF}r;ye{0mcekFHntgyBLZ&Xw%RxO(=fDFmL!Wkg;q9|MMEj>`Ha3X{CmESiB!{oz%X+=g#ry&R}r6?CJ1>h<U'
    '0s**|-~@C^IS>wMqWlx?4Efn3HtgO55OB<*Fp2;t8u#WN_z<)GiSE>oFD6{+5U=-y^7&E+R_uIoh$5d-E&=PGYK&i;c<zlq$UVNF'
    'J2NY3T6j9iGX&=<H+{RXGzDMnE@ldN2Jc|qvz>?8=M&JpTE!g;)L$Os7qM}2MetXJ^N;YW*(v_^zw4RV8<e(I>YUrP>zn_VO%4>s'
    '!OtQ&us@aTf|+OrmhdM9Un$0wzZs5wx8l-7L3pEwa;n1Xt{Pb(-q})-GV$diJF0pjmMI>ev&=QBo`|2CW|!!GBplr_%mU^rU12t`'
    '-|m0e<VtI0tCnK<vqo_&XWnlnt+GrMU5)rF$-0ad>45Vu+1OFtAe^09WoL-pebt;J<)+z`Z_@807uDLXH=dCyIGQ?N-@7lu-02(t'
    'n#qCoXkbJNT*z>zeF^8kag$K8ISZ1~cJs6U3(i1(dmObsAmEMDZ0-0uS{TesFt5<WRatUDv8J`hf_*^0mMiZRKc{l=i{IvNu6UXG'
    '-ONOGYfIVgWanUqFEp*!<(Enut9QMbb4mljvbtLMLY3H}1;0kF-$J6lzXu~&{4&X|cEmIPSE;0o2-&yBMD<{5S!vT;_O7U${1BDb'
    '0;V)1r#Hg0LTmN>N^WstD45iUsB-1snNWe)DX<sd0OcRtD2!emGRHr{W5RN4{!yUW^dycYdoVUU{C+MZGK`k-gMhMs%Y=>heqqkb'
    ';9a*yVVs5%npVAfJ?Ydzob83{Tk^h-uVIJ>=u6|@c|qI3<)QiY5nNJuLV-+C7u$)IQg2_i;sn~v-~!C1L+_thQ8P1$GYru*D24Qh'
    'k1BbHZRnM*3*!UY?Jwn;U@imvLtjIWoZAp^hxU*vD)QrY$F1?c=36TDrK0(2_t@2(?}#Rb1c@p4%al|UPbWT43h7iQo(aPr9!&L+'
    'B#8yLyu|M@hjI0)U*%#PJ!zn=jBl_7FJ!{~CGYwb#6|JNf*3%99ErH?H3p;IoWlrwEagP{%ifN-QIXFQqQXrdKi1dzr9Z7QuCs#)'
    'zBrXzUE#86`LztNNQSq_Zf|>&OX$&Ayr18CmA`*J_vjYpe7u^2C$W+(weUgexMC^$9k)bs=dS_Z#4_qP&U4KKZ|z3*<#|Mtx%f!d'
    '8AKz$yCWINzPRO`UGeMV?hmLrk)|8r)Ic0n7HEJp7yPTvyswkG6fFWz*?W=k?wxnu{VF1h1X!^0Dg*=~J}{Y=ExY$5toJ6M&9@Z6'
    '685elm5dg|<K|5t25M9RBBPu{QM#ApU_CGba6MaD_``23@mm!g06$dJ=2le+Kr4i?6fSLW^`IiN(><~ZR$yz1KN3JNj1ZCdI6X@('
    'Gf`yMlv+^X<u%O@G@j#R7q4-8CM<i*f|m>=h~kGT4_bbYGwX_m_T4%Z`qhYYwpAhmJBY}2;7JwJ;~?DeR7I%Sz96r6DP(*2Q)&Y='
    '0}*H8vKwn$CR=A~$O!GtY7N~#{mrnG)mmBuckD^4@q2wuh~?R;{9NPk^1c<xZqKj`EzCT_3nS7uB7ge$NMH;3DhB8eW*mn4UAivT'
    'plcxk9Xb^zZ6~y|r8cWNGkOyJY06bFWpCu5DVvL)0YxJFKG;I$O76;2VCYI8Ee*|GBWa%-mvE)*eQ*vRnscap-BfI-@a53Jm;K4Q'
    'WUL`h(JJxyNDBpV*WpcHy?HsuMDl4$xs*{PaH2b%toeUXO9KQH00008003MJU4<Sn1-k+O06PT$0384T0BLS!WpZV1V`VRJa&K*L'
    'baO9XUukY>bYEXCaCwzeQBT@X7=7=rXxPh4Hh#d~?z%f@gqF3{&8M4MuhI+(YYS#?U_>Q~vPhzF&Pn3pk~z?9F^VMq7v;9_#9!Ej'
    'D$w9&*3hK=zH`ra&iP6p5MZ3GvU}8M*Gpuhz~1C4tg}&c)<3uNyYAT)IaqhrSIF85!?;)5&cSlGxyL+bt+;C9xfd+GoMO(|FM3c?'
    'IBPY2xO>Y3&Tezf$sId~&D&hQbx~@c|Dq4%Ys1+;X<b*He4V}mS#mB4&hMRWvq-sh!^WneCMY{uDNrS?i*2{^eLzVenwm^!EN}c_'
    '^#KS&%AZJilN6eyc1b07s^{cviA6{^bQ_^y`Xk#}Es=VYtTqM%*`Dye8&Wu-R!F(z9-nrb8;o16wXVOoo2RtL>dAD<wCSmNY+^&h'
    '!L*r5rfrLD*ME|&Z_dtPXFu1vt~r&<+uRZppb;}q*qcg@{#Y-efzZ1$GfyeT8G~NT_Y<EoJ;PiWNPttIgrcItPXh&_m?TDH3Q}M+'
    '7NPIYJRGe_FboBmpN2>di;_Z1ybyXA{u0PMhj`(&_;8;t32_k!gz@&osK5TkM%O>OiO$t-yLRNoPM7^}^Y?2Bn%K%Du|Ea)YKj92'
    'FW$@XM8?Djukaj*AXx#*l<aHv1d9@oW)P5NJ|sk;p!fi9MQIYjcQGjOP~hNr4{%`~PD9_piAUw*teVKEww^Nl7Gy<&LP!ZC1;~?k'
    'Qy`cWpe&D+7O;&hn^G^>apZ9i8;e+dZ(z&vH8lFss2UxPk}11)>si&t!$TT{nmCRPOV46t#i=4%(v5{7WLC9wjs92WQ)aTS99g>U'
    'c^HCJ%S2iNtHxmZ<5~0{vN~S$M-h4hkUS&P4f#T!&FG0aG&qAkWU$rKVExBLOG}y5n}421Jf|mnA@tmNps{@aj+kKn08mQ<1QY-O'
    '00;m8Tnt?rSGB~x0002q0000a0001KZf0e2Wo~0-FK}{iZE$pRFJobCaCLJpUtei%X>?y-E^v80k6j9aKoo`FU4~m}B`qKbqW8&C'
    '>_;1jC^93ZB8oJj2hk}P)7(4GlUry^@a4cg-{BsPuIm~e1Dtymvpd~he>kYv6qi(-vNCqyL1AHPni8OQ5fN}Luq`~$FByrP0kz9`'
    '!r4=u(htjcEjN)2w}1MA?4qd3gXf7PZ@+$tp}~6vQ>UqJ0qzW*pTRO~^BSOE@HB&^QIc`R7Vt5fJWqEp3*??)V&K*lK?Ky5g!1%;'
    'UV%<n7wBr)4^T@31QY-O00;m8Tnt^%&bZw$82|uSKL7wG0001KZf0e2Wo~0-FK}{iZE$pRFJobCaCLJpWp-g~bzyXAZ*DGddCgnv'
    'Z(GNe|E|B{g8kAeuoc@)w<ziY-oV>r(>Ph%=wg8avesrAqZ>Pr)Mgip1xQI0Nr^AndXp4INt9$+e8`d|iVq3spW<-lUVh5Iu)i~B'
    '?&Xkr*m1Fo8ZdBq@64GqXU^ky&Kdc9zCO8{m(ywMO<>lt>kW~LIOT~}qhjyXtuKe4e)H{UU!Q0`7twgDUJ#QZjFsDYnLTY)Ya&;('
    '^MM~4F>w-NGOg;en98>^0l7CO7ZY~ABvJ{PK9%vc?*n6cExkbfK-4zHvzUl%h|;oL4U6!kNS2-PW~-Xum0H!fNX33=EcacpT8%|9'
    'b&6*_=47iKj*H@^NKAeo2!gUm7wvkIf8u@XcooCMRGNO4R`63sQgS5&QetZT98yPxRx<+%?VT9bbq@D2u~mIeTPm$MN3ZQhO>FG-'
    '`5?2tpIA4~`=^-{kx99d(&9URWMALC`*&~O`R)Br2Jhdx_nCFm0^L7=uCw*biN5M<HCN?g*`9rk4LEr)wCuc|kVkuVDS#cyxr&UJ'
    'co|WR&<P}WoRjAwT<h-_@j1+}Yvq5w_r;z5eteV*tF3yy)tqy-*4yDJ64$~ijs(N>^J>^0I~E)BVq>}0ToT0vdv}u#3=Cl=juc0C'
    'j3s$_kmNJ`MXS0ai|g&|8lNVnV11VrnTfIvv`(^^i=~{C6A`J%OrX_F@PXUQ6FjxQUnvMURfE0vcKSdj3UnZfrc*Bb$^Tzqz^-RS'
    '`Iz31H|@D4`$bIz8k*$}Tc)akq*LCs>yu9T0DS$>h=^E7q+Z&^s9oDKwGiY<*^^T~aT;pp>pac}0+5U>z5byQ?d!XC)#~queu!5K'
    'tTg@oeU@ciu}pfTx)%NGx4^APu4{#=B}HTbGh0oqcW<JFkHqnT^=9&!<>R4D)j1h&zg+aWRAfDW1LIoFN!HQxC-uy@X3qHexiebT'
    'fD_sE@eXi?#=f!b)@fb_%cM)oB6QShKG&?L;`UYrf^;Xeny2>cHY<rp<m7y~RW0Jw>_mlxcg|EYHF2`wgm<8fa&t^h@4~V8S|@4f'
    'zRbj-dZah0LOT=}@g%%~5Av0n9g)%NbgiJM5}#3;_4-H_*J$tcCh2*gj_3AL$~#^sye}#<*gEt@gsNg?nq35`#^#tP#qbkU+xcTV'
    't<-mGT}<xNo8^=C+9JI<$T<h=Bq8PaoY=TXAE?#GJW;AOol@Q|EjU{fBC^DCRKn+vt$K*RljEz+I>AL266?O5u8I(?s$Xg!2TOCd'
    'g5=gRIojRSgp=QfGC@3a(51Wx70gAHfk7l<YnnxZm0I<Ly}Qc-;AQmbGy)jLJb7m%7hbe#2^gT}gATOd1mRr{+Y1Ngj#CkFd<a(}'
    'vw6tHuD`$#8a}lRO>doM?M#kk(0q<i;cNy)Ws*Ekb%%$G>;lt(Du6kV!PcD93dn#PPuW0dZb_Dtoq4Xxd5byaI3f&o4_m}Dtx8Ah'
    'v!k!Y#(_P%FQ*DVpNM2-u|STZw18iwyo|!LOv77|vf((@sw@S&o+Fl2NFz<P>u^+I))g9!?{a2B2DZR}T9KqaU0~KkaS?jkvtD~`'
    '0+ROexlzP~8wza61ia`9d3Iy7owJw)gEen3i^n(udou{tXLCfjw)dyNi`)w<vHN6V8{EAKY&xl?$W(m3*6Fx&n#K#6Oo@0HGMIXS'
    'z=Ic}UUN!H3~<eCz-%LOwk)5`u%JQP&J~?<k$LrmqsPxx$ZbaE5pBt#m4h;wWOhBl$X9tpi*`6EV)Go_R7@-LwzWhI+w?h!BZz_A'
    'Z{)=_7^sg=iM<?sVNdj;Cil}|Qg7J@RKxrlvid%d5(w>)!@nL8pK}#EziZbvWfo>N)8B7F<26=^&p{P|OhoC}RYIA7r$5A@Ui;`n'
    'xfcZihDpR`>Ltfju;!|Pa~OiNqVTm}ho(U9;rU2at81b$E;BDh<%Ac&c#&R}#SI_<tpdOnp@^#p%cdVCh@*@g4;+3d@YRnLFsh2v'
    'aXteu#l8$bA?ty5c&5FU9k7>92uhS^S%xCA+Fsg_(Ev`Qby{i{DmYo?VeqC*@pEd7T;FNf-kx@kG6*dZ8(~B>=wi2UIdB6&VykjJ'
    'po9wkv3;n$%<LkdGT;DLge;VlKphfQJZlTkYymsqfJ6K3*^&xOnm<x6<uag%B3U2_rw@QNV8b{Bg$u4zz}8aR8-tAA7O*oFQ5_R|'
    '0r)t8jQ03;djSXa91fr3C&NnwXnQA!9eNR0Bu+)FhKVv$BTn_iB3A90XvS%8IbwWo1#{)f#M$M#rnn2eg~MoB6q?>F9b2P;0fbd@'
    'oXO$<M_PJvPE*i!JV7eJ_arGi!^r`VC5!F3Q_{6)*?VTK;5gW-)ikm&CnAcZCRy$S<_DhH-q{w%uY813V^IL@_ELy;bFeN-YtGgf'
    'd$za365jg&OU*M1P{OvnpMbJnyz)Q*Y?or1(g-;HR<$n54dt!I5RNI>hvO8Y6ihQTyHzc~%bNNmr)P0G9NB<mRx+lEf}+VB17qcX'
    'x4i>lq{Bx-qIC~NS(`o<JOc`0ynYX4G#fa3T#Q7NBhn#5OfFG~x<onQ_Agd>hFvsefIsq{4S_%(;3O&5Mh=w_&UKs$gevp?P&-WG'
    '&~R;9N}9Sp!k)vUtWmiDRuyI5H^{u@Y+YoR86vsnjcBM0T=7J*h)m)aDO@JXBSn0lGC}31OdNBewm~Ftq@G?W@9~mczAL{0I};S4'
    'U6HH~*v%P!Vo*w8rB>rCk4#C0NarAL$4Z=$00HYbsN-D>%49cQz_GbVE68D5&PD7*m}I1ap(xCQsc!Je>kta^P=t%Uk~_421FGwM'
    'U14cxE9F%Re^R861sQwED10w`eHGf|V^i^IFy-@kP5`f_ckP$c`~tR}?jWG#4)%)`d~9C?w-j{p$>^z^4*5Fz8V^$(>qR!L`a0eb'
    '=GmOI)AOu3d?@OXUn5LVVz0)Ez*0L91zW5f>=SXG*0n1)EJ%#y{62sH*z*!V@Znnlfv2BJf4IINHYVUM*?y)AGMA!=&03o2&8(0H'
    '@u7jN+~0m@HB>=<$wjnYVvUu$*-{|#416@Pr4O12_eDE~I20uGorRYkn$+;J3r(Iyhw40fkfk#5pDPoO1!>TDNKC%&HF1vNic2s3'
    '-QO0IF~WW6rYIf4Vdz@QYJ+2y#!KD7xSd_`CPJW?1xv@W<b7W*(n?1KWz0qInX5D+ckGmkJW?)|+5z~ADZ~N1f#A&*dopf2dJNX7'
    '1Qse_QOL&ya>(&EIdM^*2z!(BmG(m2DOH?6$lMUE8Qmf-Vs%lCbY4zvi1nBJsb{1UFJmPA+@BLC(>>HgYJzwH0(Ysoo6xDLN`_eK'
    '!29P2Q*X`QiZeiz&*M~O!*Qscy1R1;J)@-6m4Q-IB5hLjoU^jfYb2J*5CRpDrOXwSWs!uo^T_741BZi|J8;UAWMgj0fZ;8lE29|r'
    '1tkVLb!N+RMeX2%Y$SCF4WH_VP)DR1y3Dn3hy-5j&Dk3@O5jz{hXeBRc5;5lub8G8Uepe*6SvBb@Bqy-g7speryKxkPH+~kk4t*y'
    'OPI6E%qqe{$1Zr08_N5{s5-DktdS105~`3(>!f0&$w=EHx=P`SlB#Sq`c4O=2n?{6aQGuTMW&zG^<+jV{V5eKd5JGSqeR{nkY2pD'
    '<wYoFTO}k0cEC80<&PgBW}cDW`=SlyNIoJ*;>6dTohV05Wna$e4As`Cy5};dP!yLBFo2ooIHMp$w9D0CljPu%^CU*KTSO9FQ&(%*'
    'OHBw3{2`9O<KZip1J8a1_o}EwSb;gzTc>rPPn6b2j_iX~nN8_>hZ;rt3P4$%LeOkgH>j-RQe`Yifjxk;SizaL)01$(10ucFUd*}Z'
    '!L2u_gw$A6g++2wk(aO$IgtdHh-^U2WXvnmw6dbA0gl>|vLFSMCzoK;96q*Y2>B}~k3}`aJ1THGAR=jaH}mVid@K(rX*b{bTD4hG'
    'N&4K-9TdqKWiM0?7(f<5@|mwFw$v-9S%%d*Gq9M9%#N#kwT2){$VpM;yx9Hu4iHdlPiUJRNPCTVfS6&D8j%&|-O}UOJ1IM>%txfV'
    '=82!H?6gpIhsWU=a0{rG^IkgYWZ_M8<CliUpG7vVPgSvB8N8dAYx_~OWpQ0W4LZa2(uAmPvKQppocsgcu{YK1;HXP6kxDRM<TaA;'
    '^I45ilsWKpWs1L0G5s_kruO7w&dbU#O3TU2YO_3&qbbbM-U|4<&mx^RKby&~7D1001LcLb88-F^LLELrxKplk-~gw*Q`H9G-ioq#'
    'e4sicl`5e$+%9O<W_Vu!KrT-{pU1tAGZ+Gh)ma2j4dRjgER`u}lq#Y*=NjM@%h}o_CC}!e$7Dqu$kbslHw@f=?5fa7wJx|;D<c)+'
    'l{zb5W(7=?6uvwGknPpl`KR0W=}<bO$Q=5Z3~$+U*{1&Xk>-ZBDKpI%L@2~s>>XF%HSz%}L)R7cY5`zHzUs6izI!_Sr+@Pg4Ue$2'
    'cn?%=QcVKRtl-Zy!rm(wIJ?FzA;!J!<s_V6>$D_ij|l<R5c6v?vLok<oS}A%{yH(GT8-cNhyL`$KQh8ugjVJ^Gpm+{*i%!0rlMAa'
    'KNB;HdLx+%QUxN>7{fEvP)u*AUM7vSxA(a!s>}8&+Od~)yPIiVByVy1mf}Laj+DW!KL-<DKfyye<jsv78@O8`+&C{qm3v5{oA#Q;'
    'Gk6r@U;9~vdJadg-~zA?r|mM6qhcY`GghFcYjTwsfvl6Sm>q@=UWK^t27xjos|UIOVe%n-QVQH@rDnM)QFPMCNFC=TBC8^t<YI~E'
    '?OAlxrJRfKiR=C$dqrxWlB`xH2tDbjNNUv%z80kj!GtS%s80PB%U<u*qS^S)?Fp{|DFThoSXK0FRbN4z)NWD;ODC3aG2A|>O(U9d'
    'HIq{D=>}AU`!$rYXLdo5S0R_3EQn%Cb4sDPs1ojiNFG@au(bykNv?826I8w3?9O@(SF*p&pqCU{s@<AqMRohSJERcDQH{4!aT1y?'
    '$Y|_%p}@}T539}y<$zrJ?_r2@QZ+N2?*RC2_TkOAx!g(4&+9T%e9VRkR4Wb)-6t(!oUdAIYCVn1jU#3s*s4Y$BA_p$Bg!XbePD6%'
    'W<d8W<{|(nc-vi-A9Kljy1_x0-J?#+yK!qr3K#cwx<}ZJ`F3EHMasTb#V$^`RdT1xSGuOgdYLUa;T^Ni;0fQTbRuEUl_gc4=`Jq0'
    '6F9jVVC2sJmerwmpc(zU60YN%+j(3ikUN_L`v!ehqSc0IBpH&a7v!as6_N$=0pqavwgVLT!N};a|H&UmzkXoR9TVdSSfY%qJmaDb'
    'o}6^_grz=LvFj7sSpXBu(iGPtR5C>M8@gnoO2D4JVw=k5bQY=F60Ux8J;pLv<E_x8hVrqqm$x4L_y0WLeKT2^OPOv(+YMM$?$Eps'
    '0$)sZJ}{Onx3jB!4$S&bj0->YqlZtw`F7a<wg1V;<G=Vhz7l`{uxKRh{4Q18T-&CFl4bmC2T#eWuU+LXvbq|v%y)7=kYxyRxxKU|'
    'SMwi0_2^2D%63Tgu(#QqhZYdok-;NCLC0hOzAHiLrPt6(RCHuj==uZu)d|d=-*#UnNcUY=oYZ2P6Ek`C8fA$bH|zNlMo*4yeiE@`'
    'x^<uds7j#Kdw@r>SoL+lIojs8PIH&<)6iBTlN?-Bu`S(qg9Q|2w%UN8*}UCcpf%Li*;HIq9Mp!Y3N!;;nsD>qAd*xNf^ay+;53A9'
    'DiK~+k<zP?b{CS<eLp8Y_*BUs1nLwJROI6}VXIbR%mL|&;v$utv#QB6ci`RJXa~lq0a1UB42l|xrZ7aVMrOl5c%TR-;fA#wSdX}<'
    's><jpI?MvoEML3v;SbG~>(>m>2RBB>wi1L9!4!}~=mI{XI)h#2ohxZ{v=zG1MGtfzt=is+`8w^x{7c|QHb;0L_-goVm=>FkYs=Vn'
    'W?NlvI^qjYI>j|^T~RQGCjwHNEvraO!I*rF8B)mOxVGM2%ZiP8_v+OcUm&~i1{ou)ce+9LueWb~cIWoJdw&l%$n?cAw{>k?9OElk'
    'zoZ(@L9XYfm^vB0g~MxK!!U0_6;a`FWGu>Vi!0=vKFwQii|MOk91hhbG0mHqKU@k{+?Y}{d38}hpl4H^*o1V2+lEvn;VWORGoeJm'
    'O+9ttiUeeia__l7`2YdLNx-|CNGAv9Fdfn!5w0pZ#a&Yq{nFhKzSpC~)8VU&7nXrOb8ZQFjZWQiQ1?}M5@Gyw6^NB|rLw2x;lf1k'
    'RQufhQ|*|cO}a)kT;(vl?9<J$yD$*eA8&GqWxIol7?ZgZfK8}WM9v=4WeFuv&ZzpW2WC3*z|EN#4jK0{EhI@84$5k8T*~yasrGuJ'
    'z4StrAV5tEe4d)4Z=t3O=cg-ny{KH=qCG$3(lJvugAVzbtw}}>lr8f;G<U;T!mWB-QI`X<bn}6HU6}9hxmz&vLy?$<xl>H*&JvA%'
    'b;z+f4cM=qY1n_buL*Mrmm+$xK#>7v$oVOpd#1vD9<u{OBsqv?Dhv+nT)}1148m?^>vgWYdU4lcte<31`dAIMb+eeB(`5SYM3xN4'
    'uMQEqBL?=c^7%x_r|r+NJ(@;E0aO>&`@POTRZLh-r?l@;6g5IDu8S1ac-dceUD1O3>3G9;xu9y6e(2UB+Z>$Q-k5O{)zEym{NG(O'
    'ipY8%M@)Xa8lXClj#p8yxJDHT5q$wit~I~(8gasU6D?RbEQJ}cz^qs6q6!%04oF(EVe8iz16blx%AZH+*Crk9TvS+La!d@PuDO&{'
    'dZ6Ai&S#JP*CX~_U8tXm;5^g~EF26(V~$|G;ngU6<YMIVgenKBD@c6OiU1Yh1C6d;=$XRRdG{F7kkk#{hrZFk%CI>`mlxFrIyaF_'
    '=hErdm`BD=*<}29ni4f7x8Gc+G$j)uWY0rh)a<3E%;Z?QokW>aeeTnyB1ld;P^7CYZ5QY5xdQ1)EXu@?%W$n4DIw4tEZOU?sVxl<'
    'fJM;0avdWF_#KG=zkZzD_zlg|yLgKLZ(Qw(WtRx>Yy|Cr{4h1Sc*5aLL3V1AaG+xB&3@997kh|A&b`b_E&i?rppUA3xv}M?J*|3('
    'd|z_R35>r}9}Do@s|TFDGQ&NC>mpg`w8l*jcZo!(8AnlF-ON>mHo`Zh7;=%b_b_o3T?OH{+zw}BGw$JXg~II=#AGZ?ss@PyMm^tY'
    ')zbPJKrF29>Akz+bXs|G`~`G}>&NFizva$lal`G1<{D+UDr*N8rpy5<*rXH{@l;L~Wb_z^O;>Zhy!l7+r}`UaH16-WEKiUsw`>Q1'
    'qZAy9=(;rkufjhbP;BRG$TE=;sv^d+%Bv2!5g$2}<v2g?#hrt)dK96<R(-xzdnNW(jDF_3GW`$AK`DvB<2teNONGi3mD3Hx(e~_;'
    'Q!cA}r(~`2=%{tP=hc|(T-r{bs2hYx$P*r(AptiNv`;<#M~nHFs-qj;!D(?|y?|qad~OEaV*zBZ?o}bW+BCII_4IH}9!8LZQtBPv'
    '5pfmN!#grEqjdwy<7GWRlIj2VO7s7HsrdaW!CHg(PNX_MPX5lT0dv-3<cyc)VHf8+-UuVJ)PJVXxq(GD(Bay1wN@>muV)39Ifyc%'
    'mWgeZe$d5p^G{%W{Fi0=zQ2!wC2g*>L3d=F%C-z8_=^9zW!-E+)>3#-`Oe>3FE)yNIF7D5pLfYaZP9I0=^F)ZBgOr*oH`Dhw@rU6'
    'hP*R$NX;IzDuh|hJ@GvL_ft>NB^0_+TCa(4lVIot^^`~cudF}%M+e<9?BKU*?oWsPBY*zJKlo^9bm&*sqsI?Ne`a0zjfGLaQeYSB'
    'Gj;tZkmjENG6mw0Mt+4~*5fB%`9c4o|0=a42mSx~t$+A2{T_VeAN4;RwH|--?bG2=3u7MnhX?iIBlz<8=@UhUzxW^NZ`RH8$C`Zq'
    'OivvB^Y9a^`!5eauzY`b@`oorJoFIm>xjhFjQ0~sS552hQY77_v`a;DVc{;c*Q&h)&bBvd)R1{cg?sq*(D2a1QLy^W(^2~W_%B1F'
    '{+~X4`eejEj7M-ia*=QYTX3m#C+8&iF2d!HBUeYTr2mmC)Q>;8b@$fC_ix`F{QQgipZ@;V{o8|I-u>e9FYgcDzy0}_zr$}+P;Vc7'
    '^{c1D-wcg*@8D+lehkg(az}$BUxOvQdi>;%L}1ka<RZ=Ddd1(cZ7_ZJ_9wUR-23$Rw+HWi^2Oczc=hR>-(I@=cb#Mif%b6I-HWmD'
    'e;*oo{BRI#s*w=O;K<|AKM(b)>91(-7s!Ci?Hs}Q9?JVj;va9_`}E_%Pw)KG7ng1Q;!*Bi4W@5)*;!}LLnFh35C6kI^u+TcL*MaB'
    'md|CIwQk+JYjIyrRzvb&;eGZB8iODI?)I%amu>aJLEcWgxxU%+`)7Us3s6e~1QY-O00;m8Tnt@mDT_v41polv2mk;j0001KZf0e2'
    'Wo~0-FK}{iZE$pRFJobCaCLJpX>N37a&~EDcP?;wja5r?8b=Vm@2}WeUu<H<W#@5lRZ`_cDpjd+>{z~}5*PAglVHop0Tdrps#;_a'
    'Zx$PnFd)IoV98EcBulb@<?&zQ>6zV=|3Z3p0HItGF6_+obbtNz*FB1&NVIOyR7L(<E6C2)j@>S@153UcSbF><BuOyHLn1@0Cag!%'
    'itZV7YeYIdsP&kkee9=U7!?jkrwmEMscQ6Kjh3>^Fd>(vl@ZPCe9+d$QA7aH+xD{o8SXl<%*TEikD$_GmJZqV4_cUX^4ymj0GH5u'
    'O}cfohhznpbWp_)yB#6@eMv$1(zicc^UnV`fBT#3-kY~>T)%l+{z_KJpiWC|($PrgE$KAv{XJHWu);ALJRf67zM+W}CSbQu(16za'
    'PWTwiI4kVY_=){GjA=mkJ+Y$pFb}aF<l^+O$gDiwu3|nIS9+lSBHvr~Ek5x2{2?{)hfjU#{e4*thI~u@p!bQ(;Q0&k+}Cn26u2tM'
    '_(Q;SI|5d3mJVmrbaqOtP^0l2q69Ndi$}7!&okr~18QhF;OF;$md?rc?h8Bd4$Ly?CM5efLQf7vZcrJJet4XrS;F*aKI#-l#L9y9'
    '($Ny6*YkK-wFc%koHWUxIzv0JA3|^w4BNY}VEu3mtsJ2nT}<EOA)OZ0b8M||YtNuvV{2Wmf&B(V--}2{l>*rm)(EOS`m+5AtF=%v'
    '&RZlpte~`PBaQkQXTDS7+Q5v6Wnpv)t4?}Z(n;}ndX5Y$oRA4}W@?C)7WZk!<jR=rlYR-3uOK->EP_Va<5`)M!h3&d*&kZ<cw{G>'
    '|5tR-lhE!$uYh6ZZ(qAb>oJI}vsQr`agJ<GhJFv??}Z*vt1yZ>hV>soIAs+RFN+V-O*xxKq?3W+CMq5pDEhcyhZ8+fwqOkzMazj<'
    'f^$Rb9T+`>Yz^WsaeYokjyZO*arslkA-svYdOTB{bgOB{-j1cAqr{HwQI1(hmmH&uz)**_IlRPlY`zFH4VGIME?BL?Dnsdt>sk~u'
    'kzoVVb<IO}-*F+hU4D7)irZru>}qBep|A0hWeiM2`iwgeVs#2()T`@YJRK(0!#-75;uso7xVU5=)$F5J{7b+>Nh#9j+>elm6VoBR'
    ';rbajX=NMhg>c9q&$9c>_={@0G#$fIN?-7MvjP;{xLQFeit-4moELGfgsId-i@283m#!O(*4kljdUkuub%RmS0T1j}7K?)66-d@m'
    'GJ;G#uJoMqX9>$`@A4(~Hf%&_m1|02tpweyL-YiZLvoKdBQ~0@+oOHA%p$<J%+oPN(+L*cb;WvE#1nq4Rp83!vuQj%FqETN_VBs0'
    '*<#uDxXFZWSZjk7n_Pm)J&KUx>rND<mY%w&t%kVF^*1NG-E~SAX8zdcKQDH%7ca_JE}-UltDNO^Y$$fm7&5Dm<8;>0Hk!^LXU7h4'
    'iG@RJtTTr!hVOUtH@+9?aoBFXW@k3Xxv;SCFu3HGR#tFcD$_BnT$O)v2dKO6j^N1|9w~w?oN}Nym5URL;3MDSqI*@r!Fh^S48sEp'
    'wo_Pk6bk4Y%0*eBRSg<t-UiB@sgnn4@P5GeWD;pQFGYb*gWdH9pQ-^DjH_hU=#gPcO#RhW{JSIJA5wwpg6f`lv1q-EGZ3F;9F_2H'
    'lMFIsWY935C;xv)LP0!`{Eg%c3$9%yWO#2?6WDTOfOB#S8XNc^II*Nq5=v<%DAte9I<1Uvr`X4xm4{3Bydhs`QC;QIaJNP3lfxN;'
    'Fu#?68eD#i#@zWC%p>l91)osW|7+;C;Hq@SWReq_JXP;L@~gq1%5C_=`43P_0|XQR000O809*`RUS?5J84Caa{T=`SA^-pYX>Mj^'
    'a%FB~WiN1YZ*6dNb1!3IZE$sSFLGsbb!}xXaCz-n>rY!p7XR+Q!t#FEwbC@CjkFJSC6uBq+vcH!Y`4-%$W5J;T`-X`*`}*VHsBZ7'
    '*a_GMo0kdLP6)Ok1pLJROLJ%D-cR`#_MAD_mkSt3XcX;-MFM<h&YU@q-}%j%Yn#nx;VW4_m9hTfowbUhmrVy)D8QHEVykYw=^h?>'
    ';IUX(bB2YZN`0LL{2<E<S-#j(DmAuT6It)uMuhGANsUrTv&pQs;N@Fmd~RA~i!46PGc6uned8U|_cCw8VkLFFsqE*qVDxPx%{Fr|'
    'rH)7VeCdtX2l6blsb#AoQ-nuWTvDnlq8<Yod|Xe0ycT>SHp8gh2HseHWBsDjkCi9p)v;|_t2@?N(qifP^3K)nzTO+x`)}U5aqVWG'
    '^$RPg{)a`)KUKrqU?n+V)~Gu(U$9ujY=MXNc%sasMUef^=!nzZ|AWKhcx<sK^*V%y$6{)$40D!GtbC;ka(r=}*WyZTnly{_1S>{('
    'V#mBiTVq7spJBloj|5pU%~u+%5d(Rv-Tv3xH?FhIlz5p_D%*Cul@?%NV4E03I>{Hj+QuC77kFx&g?7!_aZ4F{%yMiJJ}b?r(#mpQ'
    '36?9BDBDlb#^8g}Ot7`I(wyhXAnXXKQ1j2U;3N+v`ASfXRdG|x8*g0APq5JZ8}Aczw(Fu*ysC@(GL3>-Qpj3<EnDYvC8gq3Dlb3-'
    '3kGz5tR{zaPY(N_8!Zim*jNEinm5e64Fpp(&7N(%ZG^<ita!dFW|!YK!q!i%BAbBm-x{O8gE7AFQslwR5Guq)D9G0GN@a%jWf4D%'
    'Z*<(9m8z^ZOA$oG#-k7sSct_SKkMV602Gz(g-p94&q8ZFxrrL23|5rMk3anW3K(;_s|!tvMIcQ4wGVBcFKXU2Sq_h2nCgqbU)UN1'
    '{kuv%dBMtZV|;0WcH*g32s5Zts>|>JG#YS%c;hxOM1j5x$&x@ek_G8xjT*uxoL93E^DHHpkp4iH)?>?O7p+PIA{7#~JV_DYVT7C~'
    'EQnf;FY1V(D4_F!s4gQnIWD88M7nldMyX79P#)M8FMOaJ0N!ay4M2C6+M0M?(={B^f1tra5j8vgen7_vNVLI5N@WUWc=0tUvUwgo'
    'aN7Q8wYi-mqeIS~A4cywJOjh7o}V5#-2+b7U1#^;V6W@muzN_3+i;D`F(lV?1h161H##`jH(=5S;)X$B&Z$M0F>AQv8hGe*j|_Mo'
    'y^D(Dm2*VhHOD`P-S4Hzs5@QLsA14mhsSw;*nP~fQ%lh}S57`yW?Ou_t}U*r+mSO4mYX-e>Fw*i+Wk%c?Y{25JGaj`UrdTW`1xX1'
    '{;N(Fs+QyGjA}9X#rDuKI4Xfi++=H0`ngUQbL|~EU5-wi@0d|4bv5Usu$!Gjz)1g&gjA<>GZsu6ZZiKC^QYO9IpAq})K(|hTF@B8'
    'k;SJhvr3O*e@$tmkmJiwz!VVUJNs6pIt9ibqUYn@{-3A+mw5WY@Zf-F;I3ovw#VTa9XZU$CbY<jc7YtCr;i1xGgvM%2F(R!^$eEl'
    '>$!IG#;xvK-}QIjzTNwm>(_d&_npLaY;Rp@F6a&JT#3bV=8f3oa~_^*Q{Ew2gZ@rnK7$Tuq9YmF?L6$|3t_WjI@Dn1S8*cIMq34s'
    'goQtE<*8jQwF3|VM$^U%TB-o&3tO7P25~zD?KVBa0=xa+!|s3e0fdl_d}JRj2i++(Ti{;sZ3Y|Owj@CV&<(cB5ZdkRDg4nEm)g%{'
    'oZ8A@2f4W}HZ#~_u1y@Q0N2!FnKcuj85(0Aic>^^iJ{GeemKn+vycs|?N2tVmY!t|y#4Xi3Mo)qvn;a&DLy~18^mVLTSrPGf%l;_'
    '^E&IF58>nEkb%cJ^kCTSa6jrE85y|m8gjZkhY3MOQ4V>WKY5Naft=N5>td{7PRrTFO8{ck9c?~-M)7`o<JQ;xJ%7L1bE~)K`qiG('
    'I~23tV;xHSlB^f1tjO4=*qDc+(-JL$JTzP)Up}bj?;Wn6e}v!BVa(<|C&X#k<ro}!jKE<2mn>F6oQ*fBfQ~Nz`Nsj5({=xQXpLR{'
    '7}XJphLg(tU?H{B-$E^&)JZ?Ke0~vX2dFa)4wEiuG)4XSfflUA?z=w@I3GGOx9>_z=cky{7!2wQC_uh3!BQ(FWLlEcdz};uyPfX;'
    'cKa!=+U*yt@5gDUk=PMdBFEHXRuofwH!N*Ao_GQC@c?XrVF@Kgro#NMfWHogA%tS51bWOM#ipPV1uYOWEf{5MQQGJoY&e45^XUIj'
    'eE5Or@SwvpDd^DKnecj2S4Xo(Kc@>H>*U&EoqITA_DJ`3bk#?^U!O(@hnU+80y#0{a5?Tv7C;@}IUVkiBXmx1))8x3j;lvJa*Hnq'
    '+|Ik6VRxtHhww-X-{laXI)0Z!p!@1qy*+>HIjwtBnkz=MS*EEpd{7l%-FmNX3{&S{$NP2%N<HtpPCOw~yM%hBwI~wvl<M{?i!||&'
    '8X0Gq9fPYrr5DX~sK~d^71kPOlLfHLU@%sys#{s4yI_n(l0aYpU=)^byVaeni0F2xQ|oMcSr+#kTPQ%7pa8|jiaI$4@}iMsu|2@3'
    'wmOH}<ksL14gyO90~KFO<r4_k+N_!jh)j}~Vi0RF^-^q1QK;Br3a|6hRHZfrg#WgYfvu0osJni(6}$qAQJpLlX;W0Gtg<z@Nxaf#'
    'i?kkd5spF>Az(oBa902mnJ+DN@+|m-HJ`HTE_sNDj7~*BJoqhv^jB%NprUQarE?{lO{qobe<h6n!8=>mMVsw1{C;-PCN{%*C{nxF'
    '<dB1M1Y)A*15o&cF!D&g;?bQVx&=;U@emnhIDX9K(7YOovc@=H@bc7i(1MF4G;kzYh7lA69d`Sn<i(-C-94T{Xs-1S0l#6c{^B+m'
    'ME_};GK?)#Tc`^XWISB9VfK<$M0R~b)Pv@|`F0i#5<(Wi4LkCs1=@zJ$HMUD55{w2Hde1;1Y%`%Cug$(PXf;QYo91gK@wVvT6#eg'
    'UcoxG6^G-)*3Kn_9=dzI02f;jL2!)~t4d`ZN)OL98PKjvm!R?_i_!oq2h`OKI2u4DrXwE0O0Dh^FyI)(=*r84`51%<Xx<gw&xIEV'
    'xBU#pLRKagRH6(%)7Lx@D5FM<@?_D3(Lq8s+AR&$<GJ4}o$J80pk3i8Km_Up@9L!lJYpdQ18^mWP-zQZe4kVfU@80Nlv)jL<)DbI'
    '<#{0sEfOpwn8Or@wX=0L9z&jg;Y0H&E0G{rii1EEpu8L)F;Smqp)&2OmL^ycvN*$vyOKctaEpeHPpQ8I^9<YQeW_syzzjLOwV`>J'
    '`Q8>WvQi5`gf5UxWPvu`l=UdBd|GCKdE6uC<6C2ZJRl5Wd$5eQq!tgBTmfi((sc>`Qjtrf>4^IL3Z#R+U6d7#8{r!b*&DP$2Mg5F'
    '9u>NLLWG3^&`GkbgjlOV;&fi1qg@YVi%<FKyprF^;7h%BVj(vB5H_9&;TI&aNvxqn2+flpbdp@IDYa=`vn&Tl6!sd$RW(0mbW57A'
    '#r#p!pC40$0sb18cUqm;61loTs(iWt!qAREL5ZC2m*yJv;-bF8YB{`mXA5Gw%%<xk300vqXJCW`;QnF80@M{iR=y4o6E5&;JO3Uo'
    'fxYB;2p^=^7z%ZI535d&;1{QpTWa)~`XY?`mi9;+Me?KdSVyi)9U3Zf^`r~&p^m|HA;#AtJheg-^iBd=N@HdFzWiTMO9KQH00008'
    '003MJT_c!rtsDjb0I>`J03!eZ0BLS!WpZV1V`VRJa&K*LbaO9bVQp}Ab1!paZ*pmFXD)Dg%~o4a8%Gp=_pcZ^57-JgPNJ%nrBsTV'
    'KD4URNU78Zf}D*#z$W(ES}!4y<!T8SFu~Xf*Kl`IE{%c>BqhdchyPM`X4g;rg`U}2+iMJMAL|Fip83w1Z@zQp%yGNjF1lHuFIUiC'
    '>Krn(Po%PKmd42BxN)GNdvYuqS6nVqn<bg7u5FR=G+^m&fv%nC<qFxT7zOpLI!(T$c^JLCOr{F;Rh1sRpi47Gp-A#GbmfF*-=3;3'
    '?7iBySsvq`dTrMDsFByxbZxXgktMm3Ia<@pGki<EoF(~b7QzjbU9K?hM~{ZYNH`c4qgYCaF)5e`#bi+$2*xee9vLTwqBs~96d{1Z'
    'Vo2#mO>827B4Pq8qNHH?sfdSzp&0xtDYx#%B<w{!-=c&f2V4k#up?a`BU+`CJ>q*=oEVhR3UXe<G5JXlKZ^%}MO2WVdn^OnwLLof'
    'iTy~5DX58Pt+1brl_6OI8cl0lkd*|wg?js%rv8|WhT%Au1%WNZgeWBxK?-5da5p31ZHF`?`-FHLOJUEjw{0Cq61dYN1Of=sb6_*<'
    '8^DUkZ5!NP#3*rF7{`@+f->mthGTd;ca0@P1=7+A(y(0O0LPFOKznSs^?gTBFy9qidIMkgb-2Ny98U1%a4{!7>e|#Lj%~8^yOZ`A'
    'Q$5KB(Ud1?kx2cP1Wd~7Al=B{S#`zjdmIxbj}^|#OQumLK<u*0aa1yA&JDi;?)ztPEQ=U&dOH&NL4qoX3YKoxp?do`3t?E0!>nK~'
    '3w7H9IK;c*_j475(4dH)Vz@WTh5n4}?H0xn;wB5>;sRJs_HiIgKf5qJh*ajxktLG;Kyu}4_0iq>;_fwaxTx2%0hBzyQx~Wo5pnoC'
    'E`3)w>gw{Qu3d_|W0%~&0`J)ww1JG*Muo0^&`%0<q!d78qGHT#bh<whWo3{RknIR<uoHyCLm|Nt{X)=?BtFG5uz3DYsHpHbCbt0Q'
    '$h5JuALzsliUWf<q5L8$5&RRRCDQVtb0_prtFOH^52r8ijK?CNL`aC-R|I7!;Sl8WmR?&mv>cfkhtxC*lr5Rrcjo&H7<r9Zrju8C'
    'IYW*=!DC=(f6!M;=Jp6fw8|vqYPC!;8hjLqg`PlN*e#CrGSTYRtf65ESjwQYr`5i@(RD}jl7uJ*Z&>CvsR#pi!E~;7gmEHt4s{1i'
    'vKYUh(F|iyQE|e#1z>(h$R_uzc2Ogg6jMHwWRyVhN&c9fRhN_8-^xN}7M>1sdX%huf)#&HV-2il)eMhLb2;;5pE)w#4!!t`V_%{x'
    '$lQ5tW)59?ZIdpQ^s-9wS-m{VlwTgxD;1i_!b_!B_rXYtM{M`aW11}z^-W#P0Kh1!kW@Oq4@P)xS#6Z&N$J=ut?>)MUiZ>5ot_}Y'
    'W&ZTl)jVv(Pr+Vf*A;YOI9Ia$;-?srv*Ce2ACf*m$q1Glx%H-yUVhJnNH+CtxH8>#;O`qKGg`_4jz@ySQmw$oi9KL3W0tmBc0Nu_'
    'juW*CXJJDEM&u-2FKBvsi*8oQ>MrW_cOwq$Lp(7scCfScphnk%ai<lws2Ur4z{f1@K};A<%%ik<GJ(GFBeJ*1B(pZ4tH9<ZDX!~Z'
    ')<6)j8`~pv@2yc?sH>Z{cIKB^XfcdPH3y1tQs~F~Ki_4sj1Sv-`H<ypaZ}ecy*5wMN4mBE{7CWGQYi^_VZFLC9%LFm#qI!d+lO0O'
    'p~XKTvY}N;ev+xLeP;a9yL4gV`c3~?ZT-e|KRw96UhZxdsg(%`(`(gSGU;Xo&BAg4pvs{6usdVv2@7BO{OpU1#eiHLpU>w`r65S3'
    'E0t>2FdIhB4_-)s#~FqaPsfVa2WBzuaol+O5?HtHf#j$^XZ@PReNvl$ASF8@rrPh)Rn~dju2VHnS5-rsB-IL;Jc6bLL+q*g%0+VT'
    'd6Gau`f;9(raSNGdLF8arOBBkRtnoAWXjGJ;OY}or|L-4{_x?$$BCHaG{I<XRMj?1e#Wx!Y1c7B#O;CHnt!1!{V%3pc8yEN<o5+S'
    'vqf_6peE0;{_lgjljH^Tysp0iP)h>@6aWAK2mk<F3|)(*!Bcqv004gg001Tc003!jW@U0^ZewLHaB^>LaCCDoX>N37a&BR4FJE72'
    'ZfSI1UoLQYQ&LjmdfL|dtb6wRJsqBTC8<Sui8=50bUy2!@O;Ux7wdbUuAT6-r<IEfNIade`uVzsr+sZtJKCPjoA9`y@%ipOPbYM|'
    'Sl{ww&juj(<=jOt)=zyoecj7NGr+Q2p6*%(lm)7PI(zo>jl1EdK5l5@QUY2H08mQ<1QY-O00;m8Tnt?ZryqyP7ytlrJ^%nH0001K'
    'Zf0e2Wo~0-FK}{iZE$pRFKKRcWpZv|Y%gVYVQh6_bZKvHE^v9xTkB67S(g9KUs3XWnW2@YlXTCHT9c8ot?YEqBi)-svs$So!Y1zN'
    'Xtt9UFf+Satz-knHuy!t+XiF6KnQ*i2pB&=nm;Aud#mbG{|oy&_uML98%WYU+TDp1sa$m*=bU>UzjMwd5D4^%$%vfJTCYR1mfdKI'
    'Ow6fFw3}6XzhQkb{N$T&NBjCjA|f&`zH2VGn~P%VOvEPTYUaCU?C!lgw{HLP-lv22Zr=U%cg^@;Lt!zsVUGBhTQ`4w`_|pN-iSU+'
    '{S)aaQ8^LyS$lcH`c~~?$<+zbsEcSz&Q<#YAgu34){Tq)Stb>l#*0Vx^>tBb-LSCkmsr=?e(J<u_O)AUa<O91z7kVer;upZmYr7<'
    '@_64aheT=;bjW0xw-L42cg=)I?>eW?M6}-DFOqYZVb?3ay8HR<{(gLv3v2C0qurWwwm0196l8%nGP#an`gtvCkDZ9kd9k_NZY_z@'
    'g1xu(*U%U-ikV`n&?nbUuqAJgCHW+N+OF-&(uTXTZZ{4^ou*)aj})1U%f*ykD7Q~nFc(`nr>7!TmAO#6mBOAPIteNB)c$_jk0@1x'
    '{r7hEP^OCQT9VgvD#bqp{}Tk*jTM{|y&rGcb4&K~x(GEj$sMw^!thGV6HaBzZcI9rL(l~kVCAt(al5`PVw<A8%tT@NcB2SQ2#B+Y'
    'TWIjO02Y8`Wcd|oOW)P^t^WRY?U{JFU^mk4Zlb@RROpIjt_Q_rz3R6}tI`{ofU^|!w1_QWX1kRX$HjIl_c~tuNSqv6uP2{c0UpY$'
    'I;Z3Ai^YJqifk0FV_ds6Nvt0oTs)~~##M91hZoLh*FsKgFTfn&3=O`)cKfU#!xhq{Wf3`Ux1MRzGf8{93PE}k+O0Etc88Tjr1Ek;'
    '+OCywYIdqh!n<H9HcpDu1t+=-Wt3ZEa(b^_s|VVrS?IpZC82txH<_XvNs44zWD)_$SLSv_PVdvTgIbm7B!hQe9m~=>@!n{Wo`)KE'
    'c9$~#@jB51QJn$n&=(P@iPdQt%gEInsm9irC@1g}tGa~~JFC=pdqYef(3|B`cYTrG9Oj+F4U!NJb*kVb#zpo}?LOv<Qtj!K3wC+I'
    '*`5%wC6=QSK7VXCBK*BwOTqIv;YAh_>%N_>i3shgUpCU<T3MR29d4hj$y}beo0@P6J5VNwhYq@25Rsy}i83&VL~Kv9NU&48k+S#p'
    'SOC0@KAmO=!<Z)jjO4=ec0C0H)O65+7M(D>%aOZqXmXs1iIXF^61h@<T<pek458suJJ9s@*@~UZvkY3#++4`n3XAF_d7j!X4;L#7'
    'yarSO#9<k*=A2cr45;yy4GYaJ$x6C2&r>;nGpCXiPZQuCwuooim5w%G$6txfLwoi>P89<I5nGX^A~}lk0)CYXG7ir&4R1xthT~Mb'
    'vJ~t_o=D1`!GDsb+6_3WDC-K1#&<b0Aw%0BK<!9UpDr?KqO=IT?b**=pMazTd~WcT<c31qG6gTXN}k<dwsRJ<Ah7NaX7LzDU~h$?'
    '`fQGJr(hpUffl(RRbmgw)DEb79olj-Es?7R0`0SL=PZjCGMy30C}c489DxTf5ciz25(8W_8!+35oUO>GGc0JVZRbl)rNp%Q!qMaB'
    's^m6f^CDd#hgJ#8behTa1tVV;5G~wjS|sK<xT%;{VYanI4BHGisbh$N+;8mpRTyZ1Pl>%8ePK`Zye<#2AX2j&0CZq_4O#s)lo8Fk'
    'h#mdqnCP6V+J!y4z9m;+Rx|zm7Brp%2m&iq5uikrPdp`*DR}xL9O~7NK9KuyEI_VhGD2i#UT|CmX`UK5M-ezH3SS2eXbSWmo{v<u'
    'wl13EGWSAMPk9537uhvg+5{5NDgbN|iFt~!Z2D0`H_FKIz~P4iU*nkk!dgw7jq@3RDE4Le30V)g(HVDrWx!rKB`8suWf_Xtn!B_q'
    ';~|_#`>gC1t2kNZVeqCx@pEd7T;EyL-kJ7}G7K#dn^8nH=wi2UIdF@360FLNkP<5B2m4TancG7^WxxTd2w5m8VRcAU(X1^%lLhR6'
    '1CAWHvt<>SG<~FA%4I+iMY>25&K?45z=m-M3Kv|XfUTvrKL#1SEnw%WqBbV>L-26`8SeOwyMTjw28Yk_6E6v!osQePVQ}b2T#-5x'
    'i8?0AT%9P@6HC~&XQCOW{q2bG{Z-5b>^#3+*A#D~w@?_bh+@m1rDJP6G=Q*5jx${v;7ChP&RGW9jweV(<m)6UJj2NWkflrR+!^Uw'
    'yy8EzR&X3_*XtTtm=h62GLtO#0rLaTba!{e$;$wt)I?mASKXxuadWsK%InVd7<;z2!xH9wfTiXe1y%xE{!c(zKVJDD0JcjpO=$$2'
    'e!JEXm8SAmV+h9-?4wBvQ3|FRn%%Ay;bl#IlGC#|9gb{3GOIb$M6set90OzJe|P)?VWh)HLZbB!MOm9Z7CZwAVZ441WHcK(e_V`2'
    'lq1q1LrgAFh<bx^!tGzK@(jCZ$^d_4o(+LOAK)Y@*2a#M5H56_3WO^2{y;lSqR?<{T1t9#eS|%ShnUVTfK^4A_jNLFIol8`%M6h`'
    '^F}mO2Cir#S;Qvsixe&w=aC|LfLOzR%EU33Y8ylXN9ya9@*Xe9<$LlQuybJ%*%Rs7fZdwmCkCYiR_b-m^2n4_h;$AMcB0G~2@tT3'
    'gF4<NpiFl2IUJjZw89*w<y_28MM*{~7>eRNi0THPybqxuA4PcBE4@qn8&I89lCd<jmGUZuKPl2D!i>FS9KIL6z6Nayu&D$zm<j}Z'
    'CxBPed-jWIegWIgb`Vf{7yM#}0NWSAEd`wdGJYnfBY}>-CZiO`dXY`Lv4MAldA26)>^y4@ABuY9*9a4!pT~;Ok{gPHELINoiIm-6'
    'y>i`x#8}Qh1P}mwz5x(?@K!+J>!;Em8+&f<i_HnROSYfsqReL~VzZX!dNV7eL40UfRvzrUvl^-(zvLlWKe5J6-DD{cc@93B*fIc3'
    'g!>|nAr1uz1LxtTk0v#|>_U_0(V;qz9%QLZ{6l5pi7*WskBG@vy(Z2vTv6$#zXv;FGC{a6+Y;p?I1F7&S!;5v(s-#m7`rR0{zM2A'
    'vta4imb~waMcV1OsEoPfKXa8v<WHP(iATz%vKxZ0m_i)D8wlRqu_xoU<0l|pL?@9{VN=M*19Hgmwm5O|YF%=^>Mk^#a@7e%OonLB'
    '_%>0IXoy;@^KxoaY`ox4JtLiD1taO_!JIgq?paM_CWsawaBnq_37wj%WQe5>ynlf(HEZ^Ek^!Q8mP8n(<4~M>+<6N<rKHu9fl^ai'
    '+FaFl&dNfsl2|4q2vk6pGGA1dMH1R6Ae+|?91dpg(5Xz4jd>*lhPQmKjAGyylo;sLnJnKGx#0!bOzRRFJ~fV@jz~3hnQP$)3B1^!'
    'vp4IMz^kAS2ju7N<or%v@|tFNQ5;+&YLy@10h(q6>%~-0IRL9U;aRvoF6o&sVa^^gYX}P+yWmZ(EANw_>cBRUMmo$&s6sC7lSz;!'
    'BW;iADupLXs<PGTI~|Z(V1TuR!ynlxGX311FEdK%&!}k0TYT|rO5{BO>CNlgeuQGORYGE52aLnA{P82i%yZKFp=d)nl8?xdILQrX'
    'H_lO0*_U%RL$x(s3s%G_6vgEO3}E6p&L{{G?{YO@k{n!mp2VoQMI_OMYqgiX)Pm4JAL0l+9=>up@cdU$uZl{96_`TJI_(2}qO?AC'
    'Y#*-4m5i=;s8OV^0F<>U1kHABlgc{Ys*D9Gu!nFKt2k3PI|&CoAhPT3V%|d!UcEsjq{gNyERvIoy?~9#i8QE0WCLO*V_u!6oz<!u'
    ';3$@q1u2+3xdfZ$@UcBZ$X_{mY^ovNagoyj5zE56nP30;6M0BUyZJ8AuFr~UI^c!wut?7+d!cf`0I~>@&wN#prCvF$3ar+dfyLzH'
    '%DBo`>j<KRoYab(7kfY70Rn36iR`cgao33kh#4lS5m|BGD?N_Ald`kQd_=lyp7@2zP8-#Bc^sYrw}5In@1>(o7hlIWe`aX>d1T}H'
    'R2BP`!F!3hwjZ^&ENv*LL1*YLO^DhSdqJMf$v@y7ds9sgj=G!>nH1ASULy@ZzoIdUG6$ZnO!22Grk{nx)V^HI`&s#AX*ro$eU?XZ'
    'G=(|Z+o6E}S!A>3XEXWLBIq$=puETq!^R##sKZAHcPb4I9H5jrRc!$7ttgAf2dYC-sS--V?SgiFhWP>j@@NVKeCh+7!4N>K%_4Ye'
    '5RdF<sX{@cTotW3&j7Dj&h{25`APwLOjg8!OdSSO!@&Lf?h2h$`+{n<Gg2Ymsk8H&tbi9Kg|AEiWc#&t{^|97IxC%1WDb42472RJ'
    'Y*T;xNOL1Ql$jQaA`)RO_K&OYD)|7Fq3eoztq8CpUv}CNe||Fjhkp$Y4Ue$2m<K91sU`tuR`6#UVec0WoL%RZ5aV8VISuF6J}b-F'
    '6GDJ>#QeI9?aKKQXQ&;czved`{~R3p!{gw{2xk#mnP1PWSsG$bO#zyUdI|na%q(g~a#f@Xv_x|Z&rm}#y-7dSNPFjitD?GWuc94$'
    'S>6&>$(s}`(RwE0DlRk{NEz(LGZ5kT6MU3I-rUHsfx8vLP4ZS$xrZdW*Iu`H29HAgbDzej=WzTIE&%&*+Aec>Di-p3#tv>YDbj01'
    '2xOgp$>cC}@GHc9*9nvvSv}AN7A7CUC#ArhR%(`;5=AGCj5TmxBDN-?X)c!d-kwEAUCOx_pSbQHvR9<`DamSeg3yzWilkO<_?0Nf'
    '2qs+7Lv`x6Sn+$W7R|<YZcq3PND*pw#;T%cyY>>|q;``+SURzUi{bW3eHzh>tC^IFPdA|=+^?aGJ-3Gic^7io>7poQG^G@pODf?m'
    'iuAE{AFSQCNOILvnxN|KW_Q+axRU*Ch5e+^Qtj3(E2`Jm-6e%MiEF%-Ns`cXK}KW8O9ggOe^_-!C<o-ye-A;Nld74~LI=QilMipk'
    '&E-yVeo>d1;^Rt`K((U4(0$Sp#`&tXruNgg+&E(Pf$drxA_Dp{I--11)_WEgZw7SFVlD=Ng16mO`7v*KUpF}DvU}8Nc{gtDNa6C{'
    'PWK4AIq!znSfuQ0RqWz~TP1h8e5Gq@td}cAC%S9a8GPXzl};oKy0WCoGu_1{ZvrP*1B~3+-?BRN4m6{GSHg9Y_d1Vj1afEdAm3P@'
    'm1wOgnrVh)>IHczWrbvce84y?zU=@-zCSWL9DMxy(Xa1YbiKql0+uKvE8n<?!PC=@p0L#Cs&->SI}2c9S)Ss0gi3~}egguz1nlc8'
    '*i<&Bvq;sJ@bsJOF_u9ZvqF~|Dksi<!Mgun|9zkN=4E9*W4aY>Hy}~DL-Rfed@<GeKv=rsuB`DnFzG)sZq+o7A3XWy+u`8X!N((y'
    '{uJc+N&o`DqM5b}dsK1rY@0SpSMbvfpOIBxeV@C?>T1NYC~bk|_hbcvTy~e%<yzrAs2*N(wnM51-e&VYT0mq+29E#*9g`vWt`w!0'
    'enTr&)sa=9>-X%Jr!aed+kKfJ+jmV-Qcq|~%;ec`l%?|AtmjJ@Jvp}dNhD6_)`13~DuGt-0UpU>)z<;%c!%FQtv$X^L#)ImIk>1|'
    'Tej~y3n<QHxqzT61-rFCdnne~R6JB1)`qGIGy`0k@bcd<l2j}N;c$w<X^0BcRFdXM>DNfR3(48O4~Y&wRq_X+1_cBa`M6Eku9q2e'
    'K)Rx|NG0bL)#RBw^lxssp)qPeG@c=YqK2X=43V#s+3*h@D8gyDVJ!#NBQC0{GP;Tmv%oaVSFeBYU2FB)RRi?FjghgfhG9f71>_LA'
    'fRCuoV3%p<N*Wz)bs2g|52q0@<nAT{o%Uhj1#lyqBfJlMHT*VAi%rL~Ww4#wQP-P}`NES<agAG76pZ1CfYfFyDiTvLCSPNQ6!JKp'
    't-I?hVsqZRdNsxu$S%D>X5LbJ%MG%w8*Km2H^}tGF|T!PTpZ&oSU;m0&SAdirkFYzzJ<ekU&SzgLlsfsabzOSZi_4Aoj%Q5Z;R=x'
    'VjK?DB{5B#nLoS{tave{WYX%PfKU%po!EqQh1-TyCE+Vyo-?6D!Am`L;fe%gj`HrgK=}Xx#A%3;EdyY34$~3s5#g%RQ`|K*(J$Q%'
    ';d?zwJRQ2aXki)XGv}6&-{{mW2X$YCClSWa)__<^SE~D39v)2ePqokEpK8YpZL)Q$;VOsWXP;h<-Gza$|9Fc-EZZGa#F)&T05G9a'
    '5jlHAmnD=yIiu>g?wjezeJ^KTIAYw(YavO(aICC&<5H%dO}QH>cj>t*L0~m4@Of&EzGXFCI6qsp8ztr97VY^NZyhsbGu9zLvpvbk'
    'fwE=3hvqSiExfA76?Hjag>F8OuZ!~iJ&y%5KNP8Hm^;O^?kv&ZtFs)N(}4Z*sfPVW2YO*H;Zj5|6)7^n3^_lAbI(<|&trCAXh{yD'
    'xhjJLJ74sOG=s30+4`L;zh2z68S5t*ls;BNZM`g}=QMeJZz4;E<5y=9x+4biu=B->kWV|9V|z4>iUO!Es`vYyf2x?UnoeooqgK=i'
    'v9uvFRO4lT*>yz=?x*7o-{pd;S^A+{i)?dnYWrixO;khkz4Cu|%_t@t1spN?@mh%LJUU**{o)!`Bt-l<9J$u~@+-s%>vg<nUAGiw'
    'zy`B^t&1vPlsh14S&3Rd#~8p8Z>7RTEB)MDM>`i4R(LsH45O}kE2;EAy=9!w9{VrH?7O;9KNZ1ws25l`7>MQ^!Ft21QTE8i$mI!D'
    '4pdi=_@vbWRDcgPyLzGL3Rf51W5_~M*O?D}qk)xSYm6>0Dh4_?kxb{(>DQP?#!lHJ{A8LEH6^!ST%$B47b9fPL!Q^|rIyU)S-PD>'
    'nNxl4(`I5wPC8Jet1NAo=Iyy6=}98aiy@ccS~XHapgCBw-(OQK4G@4u(7tjFBM0~$i2%R8pWOHj&C^`GMS$1e?}=q^5a9U;+5`Dv'
    'YI5<E!<&Na)Fk0R#n_wuq$w}<5UIR(nU`ApT^m3jRr_*t+fRGijR^U^^q3PGf2Tec;JIH9IDch^dj{7;y4Y!rn;z~B5}{@sMRj#E'
    'R~6a_-;@)`Mb6*D#8Gq=gx_*IoQ=)6hszZTuTzkai72TWBnlYyY`0y{>T3XrsJ^H7?uyfC<;n5q&>gNHU+nyrJD0@`w<B8Xl-;VV'
    '9axw$2dH3+QdGoKIaQSL6C5^O&Gqx<?@6EPZ<x`zzu&TaL8{!+4FN|fI26-$YXDw_e|(_m7V5|{krApQ#<I$*j<^vYJCc<oKkg@;'
    '!-{$ok)w8FzFmJQ_E(L5=DQO856VF)iNWJKiSaiIl_e^tn~0<C?2=QdsC%bmt@8M|eX{S@nCyJk&Yr3pgh<F!KAs@~w^GEXp8mbX'
    '{4>?jjqc*KIIvzqF+n~rgYK~avRCh_5M6DW+M#-Sv@VZg$U!Ofj_!)23hL2a8Jp3%0p;<tp6^NY|9PePf4@}x!z#gAgZNIQIzCSR'
    'E~)`@)?(z0Z_2|i&v*P0MrNu1OrdiFi*BI9wdd>YdP-l<3NLdIWkfAgJ1YI4i|6K_zyzp1Hr~?r{R0dviMjG7-H~ysJ2H~uEB+Uj'
    'byteAp2364cmCFTu~Fp1adg%B{7W8+MXybzZxncq6z|V+8aQlboBmh~d1vH^nmuM!2(w!I;#u<Vr=Ie~`9I;ite)~P_?7kh;OL-N'
    'h8^TT|NHT9aO98Q1P31ujShWmJ$&?F^e5JpUsxFRu>!l;U$3tIT(Rp<0C@$XkVbxmU)H0?Uj?!LgW&trk{k^F$G5@ZNA!E}VQ@6~'
    'VAOi_&9_g6M=gwb7#tqdn~&hjqbHBmGW;d@P=B*-Ts+pK`zLzh=pTn4Tit(o_?{K`^6{6C19<3JxUVA;Pcwd)NV;lTf0rWZE~ULy'
    'BoDqG8XkHuiuJ#FGD`m+{b^`4_~Qpp9*+cv@rbQPE|CZ|h1QyMk>)h~E)wC7Bkzx3%iu##5}$l}^Ulpr?%ldG_}kC#efGPX_ihb-'
    'ap&{jesOQ`-mTw$@f-X$xqJKQ_rH2F{LRp4HwQPm`7zX?%ZCh(d<~ND>e1uh(*mQx$Cs%C7a9Hrwn6lrTc6&#efP89-5R|6>F0Ou'
    ';nioifB8o4-*u891mfX_$BV)EzYUE%dN2qw)kuhCaOBbGABTE%>to{mGLuj>at=FudE|#k;va9`{p^#$&u;(I=Wk;D@=+eI2GKXV'
    'jHAQz(8%!MgMSYWJ@$=%=+FF;<?|+Ht($l6SlmpLwTL`i_#wPvjloZTbL-~qH?ex@AfMB2s&DlC{z>2e08mQ<1QY-O00;m8Tnt?y'
    'MDpY%1pokl2mk;l0001KZf0e2Wo~0-FK}{iZE$pRFKKRcWpZv|Y%ghUbY*gOX=QgVaCwbYTW=Fb6n@`dv07fhB}LH715zvTP*ti#'
    'p@f&JMGlv3Ye*dGb&9Ge(mKTP{X$|V#&%*m*iC?XosfjsUSIx8nKQHd#9!!{8QUaPrPeRrnK^ULcfRwTGn%F;uo|X`lKPjKQ{Al{'
    'r<G?1wt6!#KleDOD3A<8_VxQ-Q69OQmb+76xeASDS#d=*em8>pocdT;tFV?Ff6~1g-5M$y`d7aG?wWV{`{~<XUH9I+b>sTY+v*pp'
    'M*3A+Xpy!_+OJ5v;q33BKbAXY{bw>1#9z`_;(agav`)}~R(o#f7_2DE?a}Co^GAptEJNolvBS<_9U@)GM(JUm+3R$>{LWm)xXJ_l'
    'C-vTfZ}x%L=MU<E-+cNL@2?BG5%kUb4X;Fc{(?I7rD_BNR}~e12$*iKfZdg>{A<==C29U>lqt`UUkvEMg@B*m|4})oI=gF5><w5&'
    '(upa~@d`aTkhwvrPkNzIhE@U7qw8TeKO}Y<%;&BdBi*jY!^#z~w&A2f`ekNkAies|43Tyb;x&XI5)3)JFJSd>1g*G2H#(TU$3xmp'
    'n#r=|o?||RR)sBhFb3?`A^cWGLaH=KCm>#ea+f}DeZ(qFES38Ti4JL4d#9Ep?d~Y^odR;HU`E8YF}ebkF6pL8JHg{+vSd)=gp83h'
    '%S5a+zfV&ZLY(ZAUIF4SAU;Gal1A0zSsa(bdw+hxA6)WyR5!H#kLZwzL#qSb9EO>`eeD*lMj*1vnmJmFa%58~^tupzEA@ang<;sO'
    '+5ZuQQ&zI@viu;Ogu8h}+9?=px{(yrQ}26G$qpy_X&=er=?K=4QM4SJ#W**#+J@m%NLL{G9M|Vm<d|cZ8<#&u9MYSttH(3JNw*qK'
    '<kd(T8I;(uGt4sk=#pFOATZRSV+}5`Oq1Cw%&M{Ms&v6BB~}_JSA=U>%vg%`E#aDn?!4hbD5P1<x;%A7^jHcqEoSGTXY!JzYM6-f'
    '33njH>gGbISK&b3n^X_`RAaGYs2}0tl5<pXj$ZID2@55qDW7sbQX+071IZ2HXVj#nZLAl<A^mlh-e<Kxskuv&5iF(h8NYX0fRY=b'
    '6_lbZkEF_7i*hAQq$XQLSSp_jHyEwC!(Mis)|POCQPBYpEGmmd!SEWyt0);sCKeS%?7l8wIh|d;#NLLD6<X$+(pWP_H>(gnLF5qM'
    '<IRYTmdQB7eNko^U{vPGn4-xT3-1cC9_I0cUu)*L^7(8UO%Dv^nzl1|=597wx;1JtsT<bZVEG1@V0@1vr0A*}MyVwy?rF0wFLV7('
    'siM0+(1n@L`TXbQF81O@^~wd*Ja3h=yp9az?ioX7_i&t^HgswYx1V()hq%PTp*7Z-Lzct$o9P?h%JevFQLoc8o8z3BnR#f;`<2B-'
    'oR`{U3~N`_AH)C^`(gx-&+tf-Y~hrHOhdakwlE&~W@p7!4F~53v~n08*sz_zvZGKSv!Pv7HCi^IUgT|{-I+Lfpd0rCzQ^N8lX)o%'
    '{2|y~zwty52rxp)tllMqgq-?IOZaz3!9Sz|)dl4}`C`#(2WKF$8#pTA%_ixm$goC3e4hOOAqhG8K=L<|Gc36lN=WhED#x(pNFV3q'
    '7SuQJL2x5+sU(!rsi0UtKI_aPzMXO(cNQPc-}452!C8HYOG9kS)W?UX2*Ugp1G=#=hsNCb8O%fOe-WQh-T!m&7h_2|V=~SOO`hm?'
    'ANh5|(76qNIR6GvO9KQH00008003MJT}bnF<<ANL0H_`S03-ka0BLS!WpZV1V`VRJa&K*LbaO9hZggdGZeeUMa%FRMZDlTSdF@$S'
    'Pg_?Oe)q4iJTEg=nufHI=Aw>-QZ!}SgeZ{dR2nIAQzvBxOk_-^=_ry7_y#t10=B^hf(h782(}>ve8vBzIeYJOp7IxFt-X#92QZM('
    'D4K_f6gX#J*7aN8Ui)CP*(@yJ=hGSMFWxz;D0<mUfQ15lIWD&9)*J4T;rkwog+=@<zVWt^=7|M$d|T9Gu<VW3$LCsnyRI#*s@oBK'
    'jf|_l2n((8<R-kby>+Fz$f`TqLfm*&Drr%FZn1%4%Wte-boysCYte1!ywYOn`SRA)?t$LE>$h**?7Mbj!1{$1ME}F0=AWqHZLpYJ'
    'GD{@)*h0Z#5px9|+T)2bj}~F=f5t|g?%O{&JdQ^ei&C$%sVtAh)K(ehET35UN)^`erFCA5E43MtEYg##7~zQ>vx>IFh`K+^f;Aoq'
    'vSOOAG*}}B>#cVCU;F#6v(1!vnNup;cDt1}yz!2snpm90l6=vtZOk)&fv474XxA(qRc_|sL8-5^*c3b~&8X7Ka$gCyE0rkQPmyBq'
    'L1`w~T3Tr?@MI7)LLSuoGc7p9LrK086ysG?YI)<0tNBS5T6p7qjLvplw2D`CQGZIKAeIEOmS4-(`Fu&Kc$LZvkidcg-5;yTA>EV1'
    'KIleELm@U+7AtAqF!MG*Ba5clGeB5Ktjvk$yJGI?+eX;>sa0eXF#cO(^mj1E7hj4zm>EKWxCs}swY*Z9CA|dKj@ntN%4%~ILB5h@'
    '@hC(DHUcc^SsxDt0ROrdGVR8Cf;UQ#Yrqk+AAk7$6)@&<R~MQTi$Iw8YaiOYP}IC>vK(H)FsKyx394bCe^;p|FIZV_oG&kuCZ1Y_'
    'FoQUy`V<~Oq5&s}H>!al3iM@2mIShqEJ!D7)DSk|yqb-eXDPvi^!u{39$7xSXjK{zsgS7UX%zt;M#y=>f~e*Al8y+90y-av>T9Hu'
    '<7@PkNY{?9Q7SVXga@|83m*svfOpzb1JIo%wkDp}bP31gA4srJM9ogWAJ8!Z5^bPJsZ7HRFTN&0HqXQRPTL=?Hn($hY}ncJ!`K~%'
    'XK=*T^V5B&d(i2+<Ln+9>UG^6aSzLJ8}4yAhUK1);FT-xjtvbB44UM@a>F1n=hULhm?hkD4L)$XM+ZF*-$lgn$~hwLn&Y1%?)Q>p'
    ')SWJA%rNMx!{fX+;yz~BsilYsmFLRI2aD;vI^$ru(f3X7K=0M=Z*KPwbPwF>KjVBc3I5>ci&^-uI$5Y%j*By@#o!m)1IN&q1R`;h'
    'txf6YI$g}Qcj$CEI&r>ZMyb@*oR7k8b`Aj}{W}s;oz~4*Fe%(*{w?NDv&Zwm)AXvXPO`P2F^D5ePgrJ^Ud8^J(nul4mmh;Eu#E5Q'
    'Tb1fG7=MVKk9+%np8j9r>H8x?gPy@Vj-h^!!!tH|n2$|pkrV9#IYdt%3sPsWoT&{zgXIQ#uHERn*?sf7+ui;By??oWt>^l{NlZum'
    'yWZf=msmV!R>Y>B^YC<=@(!&v=<fvPGw6UOI+CH?&cj~57&Z&0Lk(tr6(<sHv_<epSorf+p4!z?I{*=2G;N}wr3!Gqu;poN5Vup%'
    'ZqqAlu-pGV;{MkFKnUqbzvR$%keyPq1?~mkX0hRIuOw&yy1{lCLc5(kfgjq^a{HZ(Q(GDAAUD^=W(Hf#waJ4W;F?-2vt|M$Lu1TC'
    'af&D~F|?V`52yK37P4Wr{mEw4(sQigMgLMOBtUJ=vCJ~0`24(X5Suw~9Vv|#ybq<B_gVjZ2p=DZ3_Q-^`y+0L`(gL!=-@rqu+!x^'
    'Ob9ZHa>(QS$#awm<g7MV7vl|cTFx$B^QHNW;{CSo=GV7-{(ht9W^d2+t39W8C}z3GI+XS$Sua#sk+Ds&u>eJ<C0Yb|Xt+ead{E8b'
    'J6u2i2>-`MFq?Os5T_BBV`%gd0)zQqvRDOiHqoR4I=cPm9|v7d*S+tdHFn*`sE$fBoK)rq8>yZC7Ha9FPWqAM^NUbBK%HT5*!<n$'
    'Knm7k_njXHoe!Lt+jpg<^Ha=e3<mWD6i{4Nsw-G(rG!jNl6tR`Vqv$_{oigs#Z|lgg7y74?KBcQ!b;?rTFi=KitmP{EyoitU_KrI'
    '6&RLKQe-O3{|fl)U>HIuc1j?}3{q?gDp8OEG1G!kwiYEt?_k3b?4C#dhvLHzM280*o=HH5-p+*Ale#*ZHTpSy@v%;>y{vN&XUrby'
    '-j442i1+K$2;mTOn?WEah8-@)J;?$n<2$FrJ$i)B3C=oVP1|wzs7I>!a?tI(;~8;x+I|R+wDDaI0jlG7IRv_|e%0Iax1Q6wH>J5^'
    'M4M%rO2Y?L@zu@u>c%j2{&l=>cc9ese(S^&GPO&nS6WLVu|TPAf3ipu52=v}mf115>QgOS6`3L(D){MnrNUYhY^nfu9ejE&Rn@I5'
    '(p@mdB1s@H05C3|pAp%jx|0<V-41nnoy|Oz#XZj!3lJtKK(Vo+PL0EQ(MYn`9$-{kokwX>HTZ*rz|z1##n)2!B*L{er{)48ljNls'
    '#9B<h6dThNDz=ou>%25osZ9gnzinhd^^rB|u3v2hufS#$CksW|6jds#Yz=M_ue7-$?Z;e%qYy<17|=Z26~IL1ON*U63qEGeC#<?l'
    '9^xURQxOmkYtE9tO0xwOZ9^`dFWGELEkeI5Vf+o=*}5*;Y?tBxXBTZ^GpvUqwTn#-IVeXUCTczag--}0kK`vF-6^75;8Ye5kx_=@'
    '$6O9AsG%rpOz=f7Pdx`IxLHC2N0Mb2!KEO>Za<W~IP|x>$5RN+wf+$B8|LaCZi7MepQb6p*fO;jbwPrRm&-QHUb2eFu1|`3(5#zp'
    'XW<|rWD(r3BVS%5HDo;&hBtpOo*TEZdJQ8GE2}#>n+<ppaL!-*L}40|&|1>ci=yxf_NlEn93QrJE+O>L-RlLo)Pe|tYphsRD(g^s'
    'c&^ESc3rvzl_y!023R?uu5Q5503tCR@eo#Ob(eqv#~?;mUM9@PAVfg(uIPR)zCgI`XD}ABGO?f%W$+p6ZaoQsGHS#qPZmuW9VBF<'
    '-O^A!p8LDfxei<l+7*rhM4(Rau3k#OD;82P09SGdmA2@`?@8qVma=bNsny_C4uaTPo)@yvBEd$2IZScbJ6C5DG35CdJ~W@Q5($E('
    'I0#e$!pi|#Ch7|;R3^P@X_6Hoi?gh_D+$yOw`l12l=@3B&#;ZYml~D;%#g!d8=Chi-`gTaR%!u=&;_!IEYK#JvL2<CPs%K?fI4zM'
    'zBLZW1HvG-2g^t$wRo`Q3P9_Vu1oNfid-U1N7Uz6ARX9u2K=(3Q4xO8ki9`0bg)4!?NOo2H$+${0G%Y;N{F=@Bu?iAI@<L>w)nK4'
    '&MWyh8T_c%PAtS`AHv2HA^ZbLYzk{A5km9igHDo9Yf5cKmn_Qx5{11+aaGNa8{LxTYcYQm<>$xMV1T~{=ABU|w?wXPkSd=kz+z~}'
    'prAy~_e*n)dT~+TVYM7yy|V=|Q)V-DS_xI5G-qLi7Qp?(j0LDGfUNu+9wuDipY8m6xCL~{^AH}S*BA<QW)G`QjzC4RsVz17OnnhX'
    'y`?>pMv?qzJ=T%yQiq0$Ts`SRe5hkEU5N3s2v4oh1bvf$meSbSzAyh5P)h>@6aWAK2mk<F3|&G}EcxIC003GH001Qb003!jW@U0^'
    'ZewLHaB^>LaCCDoX>N37a&BR4FLPsWa%pa7E^v9xR$FfqM-+bFUol!9VvEF>qN<f8DxuPcRux5}QXi7YEcPT>u-De^CI+ostpVd0'
    ';y6GqKrSTETuO<Z1R5OMA^)Y>8Lyx67kXxAZExbF?PL8AWzT%)%y+(X=FEtqD3C{4`eYvcWlp0;?IWqK+2x01{83}OhOVoLcv2Sx'
    'GLa=ye;n0cc>yTttRlLxLKhFnvkAKNkkqOqw@+ub1Q8+&LKF|7TcdI;8ji|wtY~sV32TvrDl5Zb?(S2uHX6s_sH96F6qO@-FKT18'
    '5Q@nfSY$=V>X?jE;Yb4hRrJf(5(@UCzHgAGt04iw4|a4JI0>CX)#1dTj8>TQN+r}gVSG0k1{QHiz2|cbY}ez_*-suqN<v3%L}!Kl'
    'RIHDx3ead<lai`y=rZabY@3D>DoQ~*X9a;RWKC8yT~Z?0m+EB%{Kp~f$N?#t#7fkc@*i8rF%5TnL_h#Zxd&`gfnlusMAsnt5u?Oy'
    'QJmDTOZte|3u*YfcTH%r4rS>CX<5#2fHYJc&^{OLd|#Dx%y$Ks{?J#0U2c$+#|geXE_UvNReOAf<1tx=M8nN7-Dk9jrhSG(BK@lY'
    'n6%$Rx>diU`n(vposbou6VA_Drqw4v?5xi5bTVho4Zi^HySquO${1>TB^LW$fi8$kj&4p;{ezr^l#<jaYnZb_U2y;&@xBNKxe8Kb'
    'M8;zn&W(PmKO=jkgYks8#6k#M0LRHq2f_@o1Jj2{b=uCnAldh%Sh>)Au+f~`xIlL1to=d=87Ft@0rg`tj(*Ff@99N7J^u8CGjUhl'
    'mit%Wb+>{pknwt5r3>$^gB6-7hY-nC8`H15-EYaNKEfKvbv&w1bc1koG$MJTp9&gP<T0!Qi+lfsic7Z>Y6oCvCK~Hop>EuWJUoIm'
    '{byN^;UA$av5pU&KA=0b=HeTBXW|skWFiJiM5NdaUD8K2k075`t^I{Ytw<&xL1|hI%9iZHTl-xejJ(Gj)4@}#k|%o~tqRQb-|5pA'
    '_F9G^I&G2`IwMn#hi}Cakvq^A?ucWvOm?O<8)#SomMZA%>x}PSbkQ@s6e*6u8<u%b>eBEvFrAnkQLIHypss>Rm6N9oniK{VbuXM-'
    '02cIwY;!;FHZ?+qoDLv^Q3A!s>EeTCt^gOD!Tp^o<e$UUVNX0D^B-X)*w<PE>rp+=qtjy1KG<T8jJHQG_j2U>okRBeGdsT{So_QL'
    'McJyDWU63Qo-^fNJ+!J-nlHdjW!1O9NJ_hG_vJlWC=v5z)64^)Q8J;ZaIu2Xs!X!pD9@1co?Tw#2LN2)lTPGF$-SkU<`itivp6*G'
    '3&J^cYB<3-e(=)_$=UEgpbyDzBO`_tPi_5aWL4fVA&j=ZEmx-7I{bYJZAQyQ!0||sEUm4u$HgYFn6%5Q9lKKjSR!T}a$%1IjL1Q@'
    'xl*$#t8}?e7B*0SuorRQAmW9AyPoCNhgwt1s#H5+OJ?KsCh)P#n-J5g6ztuseUL+62NBtvW0E-=&;?-ggp`)7&x;@k*c)pZy7{J2'
    'pKY4Uu6Fk4=P+Uzk$Mpn;iS+HH-5UtVm02cS(P1Dx4C7jR<rhJNOsq%%>qAC+H+Jgpf8+TSH&Yt!!ay|kmx2iKTk`4LgYrRPNv40'
    '`n6AtUv`7e<}O|e9_=rEaWP1@^RSoL%OZ6u0b%-`o->RAk#+O{3(O^eK7;YYPK~1|EPNRVvM(+d19E)=fq<A!LzqD!oo;tAdybrZ'
    'dLap}XLy$Qx>o!FFw04w=ghmuz&UpVil+m;9c-7j$o|ZLVsu4JA3sg!+3XR8LvxBQn2p*vsaMJPE{rX@egDvWDv;tkUMA3xewZP}'
    'vc3M6E=@syu|j$4#F}9(Lms=;0$elq_|VL>?Kf}Uysae^uZiRfjk<ZvGBxQ~?hB$j6x~-u(tSBQFJKr<|BLr8JILid^4lz(TqVV~'
    '(3K}x|M#c5m*gq*{KDT*O9KQH00008003MJT{FmZsvHOa0F(~^02=@R0BLS!WpZV1V`VRJa&K*LbaO9rXkl_?WG--dg;!fo8%Y*^'
    '?_bgKXkX9_O2TGWnrI%_h~2Q7Km=$s((KCe67L4>1ZIqrXoVu#AvQL)u?aTDHW!<43&A!J5^T3G|7Fru-Tjomu-~a_H)cYFU))`F'
    '>eRV>=R2oeE|<ci0lpsPv4R;~Gfp(N8DpUnz46w_zvRi3vC}ZC34T!G8y!Vq@eoVB{nX6!z&<ZFjcA><gC7Gyy_PUPykn6t-%R0^'
    'Z8iA%k=`lsU==!8auth%<70ev0qeXL;%8aK?Bw)D!MOq|SiC_hdMx?Wj7#&3@~V;9g<(ee;A7y0;)3DISL%0H{nTFL)Cazq$G`Zm'
    'mQ<9x_XfuX?@kPjx$loo4F51VG30(QHhTZTgnMG>{)6xFu70QDvVSOgtH{@?dTm9o<*kivvzn3avRVWN@nW4PD!R6-*JAc=wQg27'
    ';DBD+g*7}LHTQ!2<b^zi5Gu-L!MrjzIWs-$^*#0Yr||GxQRE5Qy4kPV*9CG`AgVVS(n-Cx#1;?OVnKR~>sUF<GsnM0cLr|Uz%vEw'
    'R=;X!D=Zb^^C6zAA{S-}W;O7@BPqVQPAO}hu$6=rNb!vl<>W<^P7dH?`oN5xAal5%CC~5R&&sE1^N+A8d1$s**g}IPGSF!>s(&9J'
    '9igmci@K)itqj}A>#ZCMhghXfhi><)BtlkJ-orEh|9<h}`lp_spLl8iPyH$_u|k7Y@~{yilaD+;&!pe$o0|1cO}Zb?_@@veukz=9'
    '6|pt;g3<(Y|FyNSNIn%8tnD>C7)N`sSgbTzGfC==(h&k@H1_z$7QtA6%Z{wDk##~=35Ivzw^0e`E#$jQxP5iZ!Z~<``*?X<I%fw4'
    'zE=|zRq@J`NwaedN#T=pl!XqY8p4AXk})LnLP4`qleb-pGIB4RqTCCte-Z4xrFR|J0qn7%dqX4R!#@nU$M21fO<-ww<Zstuo&XFw'
    '=*6o8et6W^2OP)>xFoW)B^~O8E!0Sh`}!!D+6H84VZLQFSFJ$NCX@M=*;!zXJtWnvYR;lw)6D%BX7#16ol`oM&+qFNlHWV+M+tol'
    'q@?{;@I9+U%>9=vlKU7~rizl;w*x5huHL+H;}0s|*fkdpyXxZdfuB-cu*fky=rtRt=+>A(29jnhA^mwPpqcGu2!ZcJ$sL&OHod+N'
    '8kvxAiMT{wFJ3j2GSEp(xU;2#A3=h@_jo`Q9WG^+ZEWa`7^`ej!Bx|2kEntbHjQ#xg4t}(v*YDY%>rLP6g<4hLg#=qFGch=$hBb2'
    '9}pI^6%-7~lwTvD(7+nq3a2vIDzoHn7msni9zap60|--zQg|7;>K*lhAyDiv3It;(Xy*{Rs2;OLYB`yRY!RfTaK@MHJB&9CR1K;_'
    'ubqk{%Vk-_pPCVwPf0XUk?uJcafa(#*CgQY^FH!U&rUt{x@RBF`21L!n*NXLYnLcT$jE@-yd(c_+5ZEs=g+UHw9mWRHTCwF!SUg{'
    '?%|QYj((}Yz%Xnn>;QMp=be4>*z11i@q3=zTxG2Ufmni}K&WJB31-nmfMd0<&uKDNslggaX`XCHl1jUT8z*$NvsN(6i4lYH)jXc('
    'iEVinTKoDG6~Av(Ud>Ru%Wd;w3FwiCwfEThuHITVw4`+y=c{#=TDCXz+93*^?XFs-I&rMP3yWm+TKg9E99IOjT!?nnZq?078EO$2'
    'VG40ek2^-L#Zn1yE+CFlw8fKUepIr}R#iua%5dYA8OuVTR}zk6oktT_uCw~-mFw1<qtDmbarjC-GFE&M6jP;k9Zf)hg|v%<i7diq'
    'q~9B@WfnSO$r@UOxcv+UP-R|L$YW`Cyb6Va2Ul3-18PCOT89TXf@+{qDn9@FgMnRZJz$*e8_kCGcFooxO?nmo+)k?yzT{x&gRZR-'
    '6*Vz#tRPdp`t7Wlo#g=-ZfI{%)fg)9pb#eln!VNQ3^Y9mO9BLTmrP`g*pXffV3d%|@-Yy%w^9k>HhYXn&}?ecLBT;CjRHK@1dqS~'
    'XyKVNoOFgT+C2xw+Ji}#QV|{v(-Ao;Iu#rxGAm~<R6A3SK;YtSGWg1(+6Kj~vh7V4DR#HIIzV~Sdcth?7%8?pNc_hiMXeCl^>zV1'
    'I%<tf4wUHBDo@vO0&XE;q@P-XiXt<#2zWpXnVmEk977OW2op()MoT2O7nBBG3`*|~<ilf+@4pXco=xMBSvjUqh^bA+fhgD>@`Tf*'
    'vm5+;5A}McA5MX|yzYN_W-+EsP5*nQhjl?s2pt9^*@2N8BNb+;CCa4uD!871@d2UsenDKrz7Tic{eEb0<V)oHl?o)V#gZTiF-Ej9'
    '*48`cI|Y^!gkoXYBB`rZ>k7!4?b|cHloKt;=XvJ#%=+9HlLy;*2^S@aOQ-EYUY=s-r>qhOQmu53McyLNdZ&U0Y)^>6yo|97lo+z?'
    '`68S^*^z9zE)yhiMQJWj$pL&;ZpW;&+0FuZ9cEIhqbvBXV#`e$i?tO@zh>3$#4PmA%AT6RMLG{GK{eY+NQb)}0fX8WDoW1dcCadI'
    'fGWg(3%_!vJ2`xiB8(2y<uegLm*`jw0J6*LwQbg%7oZGKFB8*;p)HGnKS0IqXt^y$e2N4rMd132pBP=RUZ7gnUK_<=7t_uhaS6YQ'
    'IyWaO)b*>!{WCr=T;Hlc*<D2K?sb2zU!Qnq>DvrKE+-1oe%IHRMLpBA&%8bobb>?OvOAf%9BIIw{{E?`b^-h!P)h>@6aWAK2mk<F'
    '3|$>!KQc8F005Ih000~S003!jW@U0^ZewLHadl;LbZKvHUu0!;Wn*+MaCy}`Yi|?TlHdI+dVQshTR5JCnL8^*WF+>IS<Q;a9bopM'
    '5cPEIcGBS3PIsFSc`Z3GnS@6kEQHL2K*;b2mmv=z1BsmkX@APa-L^mR7q;r0)9<$P0%;d%GTnXZRMn~1sj71v4u?ba2fx);XX>*n'
    '_1}J}F5fj4$E$Z2jPYq>enP*o7z$O&lbm!}OyxvP&ZJ}6Va}ME)~D`$+`6UT8r7$N|F|_5YUT8eXWa2VDXqzIPM?}EiVuwWl0N>n'
    'I`@+wH^9@6`eeC2Hc{D_tgSz<k4+hmi^jrJ#5Ovwmu4t%y;!d8+zy3-*N{Ar%_tgoQO%^y=TxSzPfquluZqNpDkmtC`9YHhq)<}H'
    '3~&iKnM}#O92+<%4d$eDTskFc@qS4OIT1<<Vu2@NAd{f8<#bX~5I#0IB&CITN`}RnxX3335vm#m&uMZhrb$;c0iR5`cuEq}f+!2P'
    '0+^s?Gig=w5*W%TmjvlbR#Ieq7ZQ>t#VuL@laLggoEwk?6-GcWp^yOFa-Am6G5CLk4{4&(Cuu@Tx-6yWAj&FsqU)c3_!lxGrb^mL'
    '9E~_Z`A{;WaQ$LBkpfA`X^w!hceNwL!9Vua2tnx7@&6Fcc71!Y>o4cOJ9An%d*+AJC(Z#6M?;~%oIG=`>xA(A4_)WT*k6yIKi_rs'
    'G>&!?+)9h_%c?&8tR;$%^Vf~ni}07c)vj$<HYVUNd8=&A!5jP~Z~D|Kyy4&S)0U_c(<~id-83J`*CqSwl8Ky94fTXV$4{U8_pY<T'
    '`L0ucJ$d|mm(X>x>r~h2^MuKk+WJCm{V~jG(eJO-=XUVPn0#T}L$+F~<vYg1dGZZhWn!$QCv>*!`yakL>s<7=-*uflL9r3D0VU!L'
    '^^%-QsrZzT)VLyN%>gm34oM223Wee+QB}D!IW3#hzR!s%SsSK00UtSe@)TFwxucJjs7uwBuIj4`lq2Fo2$ztOoFK?)SrdeaDy5P<'
    'osa+UHI@YeQUaJeujC~6kgTe}F7n;u9_W|T8vAY$5fcf4OdwRE0C*XU8rbDjO^I^2MD~cY$s=G0hrRqzqA@X>1>=uIOf(b&&s)H9'
    '(jvii0wPIKGD_G+=3n5qVoH^q^9dvV31oF91Ng$*tL)9mse};EC<=8(Aq)PT)u?2-)^BiG9ZnkQjOM`Vrlv?*PDz6dtgi$}z*vH^'
    'xsVRWE@tF(BpGhy^8JM{M|>5%)JADbgrQ3k-REsog-~tvoj(8En15-!T%;CL*;uX@rz;!9+Vr-*@({eJvT>vGe#KaRT)nyp0Uy0<'
    'XSO~vYivz@C|;#8IYg`})2m9#Wl1n?SrszGJ|h^XQlQ#|9M_^GatjGj6U`4MJcuuc7J!mPkp){fwPx3rMfM1%#fO>b2I*XonG08h'
    'XQPZy7^f`8AbLf@*7tA}ESV`8RF;y`5o?U+j(Hik1kT9DY}02}7<H-H{Vy4&>)`TkIt8viatBsPWnX*yrh0$Hcr?c4-P8%#E(G-V'
    'qxHpdW#g&wXp8OeBWwE8?Lb@>Nl>@GcDquVq=b!`HIy(D3mTpbDn#3p(y$vTq9!@Gw}pa`Nm)uI5VTP&bo&8wlR=^uGMDf0E$~3X'
    'b_o6D%e>4T;o1vmNn0gYDu_v6{9Rw3HCF!MTJnT&q2(a>sK9BCWMkqeYD>D6jZ*b*rTt5k?h|0VR6t?+&WzR}Ls^y6swP4j5uqSa'
    'E+wm)N0vzUB82BlZo+g%fZV-^qZs0N4pIW*DC(t{B4tw|jD<Pi2?9FL{X-}BRY#y052eKb$ur?}=Pwp|I$QGUB@jDE8~6PC_bVGO'
    'Kn0%yHAgjU{Bftc^*7L?joj9NlnT}Xipyv3RCEZiXYqYX2ViR>62pfBN-Y>`Ghyx1^astoaL^sHsiYbhKb_>UQ~HtTzTw*2+T6ko'
    '5bThsq#=9WRR|sd(0$6dHwIfRi|>?d0b>CKg4)~B+WRr!0AB0APD9ajFcPq#S6}Z`SBkZ<HxwJ};QG&_pH0GuFe_@B1PKP<Y?Rjf'
    '>>O$rC>um2-XCGpqm1Nd4)HV?>uV$R*=2oes<t{|yc&bp%(ysAoHy=nqE&Adjk`O5nrg%$EAwBkB8iu%K4=98Qnr#JDsB25WSuY2'
    '`J^5k27##pO^_)Bg~q}E#4%J(8>6Tx!?q+H(?RPn9dfLQLkCLBg~X8-;u;Y*DV~q@DVbb0!cuJMN?ginT;#Z>DROU4le$n7QSRTw'
    'RE|FLO~|%c$&_d4n@;XacdoEEGu?-&GQwf~$&$YOER49>OpFDQaAo68Wn<3y9k>YBR(Gn;Z+gE1g39J2$Jftnwdlo!ZOY^qlq;~d'
    '%0{_bdJi=wSlfXtxA<$lc)e08>F-`ceVuo!rJ!%F>+hacw&$zmsrrjs*zj?h`a%iPLJL5j{FyOg=FGHP0cI_c77Y28hCJL<7+QTS'
    'ksHXW5z7Ihpm8;)NP?)wWx10SXS`!7u+=LmnD(d=wc(_kPKc=#G2;AJ1u$cq2(#LF4hplhMpgzhO^#yLLpn1Q;bS<={89vd#NL!d'
    'g#mN&=o`a4vCUyrydtLiq=*Z1D{LZBH!+YiU)XQ(rG>t2(e&M8&-A0kUt?)dKu3w(597JGoCfj<YrYFzt6SK1%9&HMa$L^jRBK)U'
    'vR0q`T-CW3hm|sV26#_YIFwU)GkwyEglZRR#_Hr6CgStYv4k9jf_J$(_Y+utrM%q%YWHweI4~)|YFsa3FKhN~eRlmraRh29V`7E5'
    ')PtwBu@|s)gB$6S3y>`Af79NR-MEt?3gStMn8iR+^ra^!8UUd~0whQ&X+UDl9(*QDk_TY2Vc!f$6Bw-O0FILB3Ce-Jbb@T*kU3gI'
    'Rq0c!wQJkP{EPa?ANtM<{eDSbnIrA9pAL=`GBe}W_3G$Si!pw?vUyX#jusb-#b_T99%vg89%vU49%#!BzmOgxJhXrfQBLY(6UM!@'
    'Y8hS+U?|u#v(3cOW^8IJ2<NdB(tHCu`TDcv&YXignmZGk?#%h?w!M@l%`iJN=db;VGwY$TWtknXJ}FIP>}YwhzMo?|19<^R->|pQ'
    ')%hiFHMKyYdb#9pYH<{u+0XK)i`{0b*y8}OQpA8$f(U(O%K|_t-KX`d0(J!ad8jq+=qlKA$TILu0fV;-lNXXTb*;u_cQVXDXl0#C'
    'm2@s$2zq$3Rk+!cAQj-u2KEpn(P>F-#JM@_VBDJF<lBYD0~gHo5aW6XxpqybOuHF#@l3S|iOB$z_8KbEG>#Z;AVXR%DVJqw$Zt`+'
    '(+)j|V!+RUs6`wtUsQ6ZZ4S0;STgN&smBH|`IppArz8R}Fgy0V`!O*%_p1$dG1Dt(vX+uMJvy`oXjF2~a5!(!JYP(UI@+@?H*~*J'
    'Fy8cVTMZ(lq^dk1r$i+(*v^hD29L4_tsi9FpGhS;xMV6LqII>!{^`%8kKLQeq%2QoIrH*t_2si_X{maDqq4Q7m!9e4PmFuZm5nE~'
    '^-2%F>c9U2Llnl00bpT+?Rc^Qw7o5U>!=1SyFH43k1{hKZ0Do&;VAFuXA?wiSkh2fc<~{%k4IpR*sDg!yjC)U=f2<u#48b-9&QG0'
    'v9Fq7fkd?(kAbwKENdmakq5@xMKK;1m4q2t0G|6tG6Bh1pZ<$}GC4CR9Ru=zZbDwcLu^3pIs1*A&ZH$!XgeyEj}a)h&(VpHhFH30'
    'Mq=#(EQPjgW^cPdlvCt{eeMOe1YV5@p$KwXQ=7eA?CYfc<JNd>>9O%}`UpL0KSDeB^q{@=a6+FRhfIW`fpn)jxeB@J$F1w8!I6Vs'
    'Ii1WzlHokCTHyRN^sS=4gjI497sG;HLol{GNK#sGp246Au--*R6H@|qWFX4nQ1D4I?Bmw1mqH<-4#^j&NnEJS14&m?q*qiW7F_}4'
    'aGSZEGWo>?*<1BV2|G<>m)BQrfhat(3Wl+<nl+`xu7mlI1(lYd1_N_dhJDAH<f}mIGGb>TSGR@9+OBKeD)2X|%?aS`BpJk%-X`DC'
    'tm)NQQq(?bYuBoGpBv8?^@lgXcrj-)sbS{!Mc&O@!Faid=IRn?GJGgp?yiv{4q>wFxZn}-t<S9>e<li?b;5uC0d-gf+H78@l@HjN'
    'fD0gCKJ32Y0PU0bM;)=G)A8&E<abdE6<_WBUH$$;^x_)}0ivXm!wwM@JPA2or5pki9}3V(@MW3^3<hZs{#vIhEf>g@3a*8Bd{Xce'
    'd;yut*~qRae*E!Aq{}8bKYQ@(BHs_iX?<suuJd2dojFa*{}0967t(nkSnx`5S9m_?7!^kRZb_J7m6^p)fI^&#gR)3p*<`1d9@h3V'
    'l@B^oG$$YPZUU`bd+^a=v(N}UK>B_m4HEnN+@p^hQygAr*eKSvS13c274CoDvBN2PivYGn<IN0*vh|$Of{??3TzKOv(D4unp63eL'
    'uB$dKH^v^Nnv$e!<R~93g$!f^>452qD1&C~_I!jnq5fnJZ+-aG0t+@~AHnjg<)vzQ+2?n@z1HF}DC}F{q}2<#OhWa0r+KD8U{L5M'
    'q<ALL^kktaQL?GweL5BuvZAcmSLg7J#ES-c#Bp*j$N}o-x#<EKj^G)7|97%kdB37RnV_-JlOr?)^V4Ax?IQof_J*i(Qb%KUNDnob'
    'pUnko4wPaCmzR9;Xjk}PK^(~2sjZvWtVkvUsZ4->6Yu3{kc4zfAAPPrc~x6~ioR~lzXS+&9fn+P0>2KmvRiYkAN$K{wX|x+fa<dm'
    'WBIpAxm?+v0i5-*8y|`zr$psaA~OWhWu#JmTzxd6KbSQhjn!7x09SQ!rSkp(ZTQl1ud?yFvi%;?k=o5y5U=$q*x+XLsdw1Ly79`m'
    '`4XP-p4H+Co=r}V0}}J#oLm?Ijv#ffmVZJQU7f<S^U-V7^+))f+~cth)UkhUk_?5z;l`3|9y@J8H^ktLdnhj^bRzD?wD%VI_7?q`'
    'M!>)*m<%_t4@gMxwvNN5AsGQPh6vEzakQ<i2h5p97s(wN|HRqFJSnvF&9~L{)#}o!H;-^Npd2^PtXrUX`Lnx+-z_h5tiVjBZc)>w'
    'VZAeN5i8KPg4c5ofWfmLh=~LVmTkVvM5GPPmWLgNe1Q+VUl-8S-}`~C^O_i$)v>?bQC5XSE=!KTh9ix-J?!3ybVY{IbRRtfG~ej0'
    'OJ^y|2j7)KLi^;-RmPe(PY3<@-Sn~PZeB3)qa^w|(#cBaCbbx~;pxfx*jTmnCQl}!5nwc2U#+byl4iV%MIH(&&s9RamFTU^J{@L0'
    '=<LWn5j!@GE^0PjJvJ`xuh343{_b^cX9og1@>jlNJXoOJ4DY)3G{>)zCPjOb;`vW_F-w24!2SK_|K@1ZfbL|hH%#)@o8ZVG>e}i|'
    '=ujK4+7GFd*oO^kw<u{F&?E7HY=pXs*{XM5VCJ~IT!?Y`LDp6GpI_mmh{waaTie?@dZJuA@2BR<4w8-1gGza{w!T=swZbv`!#l5d'
    'HajzJ+*`xbA*>13=BisIE^n@%0C^LL+TzpN+cz|U0JXDUtPoqk;+7;1Xz!S=-E($yNs&7j%Dco<04z@I_1_-oPwu&I>$=#fY)oRU'
    '@qSGopRFxEr&}4?#`G<02NFI4bAW^I{hmD^%6ek+^m=jxBxZ964D#X7{<r?dTRuA59_v`<g!4tbJG1rx+yKkbnKiP>6|3d(pg0p0'
    'unvl6PtG2)U7G&p2@Faf9kttYfVfwTUvl<C?cH`jydnfad}*J;^vfwJC@^|2u#-NXAJ?&98lKz3zG=Z*j88?5wlnMqmGT6d&mMvd'
    '+b?QM&-AI=&fkjpl=tZ=cv2v1?UpBD)S<8tY#=2^AH`o%++;s_bNguZ?jP0d+w>ZhK3_6^U1bNscmsbf9~A!zKEtX(oOyNiBl$5@'
    '?b>#OfOD%>yr!=%P}2BQryKXHS2z90NF4;bmp=A0%`%NgTl&@%vXgh@Zynu^*KkKT7K??W<~{U|?l2T2P=`=D;hw1LqJas`?x@h$'
    'LZPQn2s@k8E&=bQ5$n;$|L0DuXWb2&oldBx^PR5;?}P3M>A^aqERlDj+-sz#?)ma%-WQ0xEy&=$<^B-_B_1q|-@PqnFEOBf)L5ba'
    'ml|bFm*8xjxt4SIR@FXL?`i{)0za^YbwDh;^kRc+VvpoLTT$$|-OL`@y*u}25-jVzmY(}8KMz%=xyzwl%Gc%!r8yQoX2p}H?K}GQ'
    '3A}ePv(_1u&F#}(_Kn^V34_IRtf_RGcm_>gp+u<vI&CGaQ2o&Sq(SBy&?-74Hpc?Jhx4xY?hf}ldz^A%p+;88=|e~G!9>0ga)0Y~'
    'I9HFCUZ}~zeZU0<Q=@d@>~}q+`q`7&d;*LPL7Ep+$SOmjJw(%hW`6)nW`NU%vr<IjV}g(t2P8o#1cuK2BfwT#TtHEGV)5trr80JD'
    'L;nj<O9KQH00008003MJT|EK%T}uN10O|$+03HAU0BLS!WpZV1V`VRMbaG*IWoLOWUtei%X>?y-E^v9BlwC_AQ5eVX&!;%pje@Ia'
    'T?s9?<t{-lqIMAxVm5|g8Z+ZUM4-E=xn;qwxl!WU(t-tA?LuX``CZC$Mz8z|`^=niQmb9q!3)nkFXunM=S8Q}A-a9kiXTXqHB#7?'
    '$~kHO2mMtd$(`m+nKV8kM2l6LsbHS-^U<K_71vfd+)AC1Po;J&O}9_Ud6nE|rNd1YOe%%u-S$K51ERSL^1Z+klfAQcKGvxpB8v%='
    'e2JD%XfD;PUE$!$YJ?XtIJ0>0M^?oLdj*mAiQICK6Yx^dFJh3yw9$hix!6QHkdFd<WCaJq0gmUwe$J%EteF;l0@uy%V&dJZ?Zud2'
    'SmgMyFVu~jP|yDi^!rv;qe3@qOgU*mXb9hDWAMYhh@|=#Qa<R^w@79mj-C2WJKi9POgoh!CmFJJL~6I4dYVNduO@Bui7LGTc~t?U'
    '19yYz@%FwF-X}k+au`h23B3E<q}sRUO|1F5A!SSO;Ub1&f{$9&%3-sbw&5}Rlylatw-~S)yJyBG?2~${{C{HF<8V#GX;?8ioD*+m'
    'Cg=1cpw~0!ba<!iP8+N2C|*Yx9*YgDpJK$yGlXgJ7+O@ZTEW`oU}RS5<PNMr(K9nXW5eVw0d%F^3+#^ggO7*+q>^FeX@>T3RXVDW'
    'tt&PN&!lp!1{WitwZUA&mZ!-`G9ioI=%L=O9kePZ40RW>cii#TK0E30x;-wt$MJ3s0BETYLQ7l#FLPpd0M_)qHL=NcFyL3O2iw}F'
    '$bzAGc4T+2$zJZ+VMR)uonIDM2Kni~uy;q$VybnXgmzWC0f1S|R`ZB?*dmirGPpP~%7=T>Y4;9l#!LdFqxmzkQ*9?Q^y^itktIh3'
    '29qi6ei~xp<n&l;A``IcLuABH2pd$aJnRzzK>vlU5qE92LAeHvG3-AYi>vDhmKPvpc$8@ZC@?V1+g;Y553#t`W>Ps#)2HNepZROd'
    '{+GWN@}g*!aUVjB-F)v&3($vxwzd>rzEG%V59^h_lSg(QUK$5)TF~&Y3et3p=F{Z*C*!1Uc<2K8GOs_f#P$%=$H)!Tc>VG7Oj?i)'
    '3)<^IgvYRW!&rbGc<hMxzED(_Yfv0F&>v7s0|XQR000O809*`Rai`T%Ob7q~_80&F8UO$QX>Mj^a%FB~WiNAda$$63XL&DTVRL0J'
    'aCyC0TT>fH7Jk>Sn8FXXYl&iS_K7NQS)tf9TQ<0WN*+?FsbSQDSxX~!W&|NyTP0x-f*^z!ak05O+;$x#fxSj(B=lbrb<gO@e<7!*'
    'M`9!{5|FG4p_%SJ-QW4n>2pqZOG}H3{IFt1mW@Llum4~iEE)?tcq0d|E>|fV$8$5z{!jTBj!r<!=VWt>{*<>)jFl9Qef*T4GTw~i'
    ';}p)1m9o0I8z~j$joA!d-T#z-<NAyvi8*6CXQtyY0ZyGji;)WB*(h1aka!9oERodzO4(VGJt7<PWbp*&6EMb1kC*ZpL;rwtyL7_$'
    'I)yM7H}*2Wgd=3_WqC4AAC@<9W*jhp2wqt?GcWOK2Cs$b>+w#huxjWDSgZVY9B+>sb5V0|9LHX}Trki`?w7;5dHM<_e){y8nLZ|m'
    'V=(34db_*Ky&uW?9K3i2kgQ1}PQ*!g%{Z8Xl}g1mG83T`C6+(sXN=?_o*O5taXh_+5629Bqm&J^#eNBoxmti|mrn@>IH6zUq`**6'
    '(KyeS?XB>xYKkCg0i+EEy=;tE&;-#hs47w`W2;Rrm+SG_i}jyt7ySNyL4463lzq}bn~Q@FtmtH-JP|hvDGGyj4k<yO<VU=M)R13m'
    ';{XC@eeQ5ZQpYb7I2HxX4Sk-hud-<@IZ4)#GAyByddWLndF`#4ULeyu_|^0|xqpO^s!2il+=3vtlq$bnHV%?xD^?@tvX$#Wq7ss{'
    'USLT>1EcqVwfj{S&pR)fE|`Z~6&I$%$YKV+U3ZQoOW?0FIPu@|YM6Q_9re)78TuZXD%LM(_5RV#nY&SAWs!L}_$PV403K8IYH$+f'
    'rPLcOA7*Z1Nq^zyU>{JWtr<V(c}bQuo_7z8a^(F!j?Um%hU4vi39RmwM9mWY6B89J%ep~~BgZ<P-o)t>oJ-Ozch1NaZkYCWxSx|@'
    '_~XpnyOLfYo0DcbYPYcwkHH&;kfJe7AJ;h~ZSx`@r;QFFUJQECJr((V_qoshO!Z-guotw=kY6-5Bb49WG48JAF|y#l+sfS)g}_iq'
    'rBATp-;t=@t@tDTxZUn+<B^3xLp-JI0a=`6F{6Tt1u=?*#WDI+4P_Ujs`BV0yq3pHkPaY?7wsL&hZMhhhWMh5YlAmuk8xs=to~%P'
    'dXc5C25P)6fYvy^&@@Uw6C6Qa*rnDE;3>xrv?|grfQTm|W-1H#I9sd_q&OVTmsg^AE=D)WLI&ddAK+{Jv|Gv^;k8MeNW<o6fZq}I'
    'Z{V=dh~xonkoQUfB&!g89IE&wRr>};d|RVXDZh-Ti_V<G+y=*%%&q-fIO_-!UdTvQuYm<m|9EXISD3cCFwa6s-SRZv)U7`^YrIRd'
    'm06x;K4)*CZ~oarH(yH|*8*wK&<{xR#4N6~v~n#YNE#T_pyz@6;e&s*QheWD8!zNR`6j5R;#JgU<PG@WWvFrD6`9#7W#>!TH)J7Z'
    'zAV&W|Kdi-1Hurmia`bOTCiRPX7N^z?sp`6**LY$3iXys^x;wy;L|0@$=uJ;e{)Pnrk%5s6+zd+3t>qN`621>J_&*}!_)|;&mR<O'
    'fs)M=TEjWGXC7mwwv5yg_wd034)8%}C1D*N8f<%37Fs#9smKM}+=~>ev<M(xg}ef6+e+Atv|w^*P@gDpCh9Ck+1jOSuAEFk9D`H~'
    '2?QcBgtF4<3I|mpEuaEJ8doY5p?<t9l3fOPNI@Vk(s(cuR9^7tJBT0>lEn)3Rx9=JHp|be4qpShTFg2tT&QNu;v~_xp$q}{z$YK!'
    '!7LVN#c;d>br0l!<KVP`1)L?|q0T*L$~K!tEcAjnD4}5lzOV6&EL%5b3J`APLMoVzby_VK+J>x;V?D=ogsp1m|DiS4Sl%|lwF65X'
    ';}vk#Ni3ArH|9TIgG@(N#N+pO%D$iy5U8UuU8(~8g8cm;d_zGxvkz<xV+6liH|+aCNrvpy50lGQ6;nUpqJ`_nP*pr<JlFNsv&(uK'
    'a*R(tlwZ#=<KL8H<PC`yIkl#@5Z%oFWZ|_rcEo)AcG!l3ehH3pG}vi^7E*sZh{+;3(&;hiXy$ehHAK&Rf-=g(`9~U%f2&PIX2#42'
    'VB02(2UlTpRA6}sB6baO{XkHyz-q8J7m^ubR*>C}&1&s+V-&0uYmA~=><<bQ3)m$P)TCjWUTfBdS_X_ZZ0!)Wq0z#7WHWC}o#4|*'
    '4Ft$WWqsL9ZR3f<Qtlm2tsAq}0Vk1WN7cHtWZf)CvJWXVmblp@s#^uH$XmH)Xn*4A@wE4K^zhHR`#Qh!^mXvhd%B-J@8kPAo;`mG'
    '?~Vwig%r672{((C_iEIBRP%PfjzEB>pg~$U4MRpB1Z2=8$phzj)GBfBDksXu&5!T@58Q#)JsnRvx_Udm>fn2ybocZ@Q)ky-8&cS;'
    'po2-O_#^wbj=g?Cl|&xU2Cs1W2I+Pq2lY;=xJ4pqNVjmx`#p*K($m}7&UbeGrMnSRmyfJa<52RE<GGF^K~;FskA%7er3fRet2xKw'
    'JiR>}D-B6@f_zx|JxPMK`1YqAo~}k5T{^HzQLUDb>fY~P=P*eR2abJNTSc7@vXHr|$01hRwmR+<D}cI!GHO(;*hjqdI6W0Llgrrs'
    '<TbNce@OCs`7?x-d(4_-av@4~H^LURuQ-z%BvuJP9NFI@YsKo7$Sv*`&d{l&DwGZlp`8asFK?J|Q&c*y?3*qIY+vw62pEM%A>`K_'
    'mmud3zl=b3EMrRnN_$&{^OXz#7%t4)z4;v8K7<nvy(ybm#kqybq3zt^>{n1r0|XQR000O809*`RL5b{}N+SROlwAM-8~^|SX>Mj^'
    'a%FB~WiNAda$$63XL&DUVQp}Ab1ras?R;r-8`qWgcm9elq*5g@GDA|9$K#ZyM&)QSnm4ii+L|SmNwH`Ol3Afh4GG2ZxKssD#7Zor'
    '2(I8N0o)`2B*g{9LjIQ+x4ZGlf8l-3Jr~VJ1CS`i?|d4SEdkwkIrps3Irm<;T&@m%eM6s2{a6lt+W+v;<FOyhVIw<d%w}r4+xqHp'
    'M~A4)h;Xc0^6QDI>RC!(_KLlP$gLXjJ$)m9CO?*=df2NkhxPS>IDe|oo$9Y=#Y#|L51P3>?e6El{v1s@#F?L`suq?+WTUq1)%VBs'
    'g}AYi6I1b;cdA<06&o*w-~WSmyu$^`I_{5-Jk*BzhaU`l(?2-e^$1@ddB(KChmS@^J=&+=^n1RLkM6o3J{s!xxH~%3tH++fp{{|E'
    ';roLR`slOPKc?Bf^1y?`_uZrJ;Q@Eom}eBdAAHyKb^n;#8oqyM=<EK0KYc3uUDLjF4~#w@^n9h-c69tqF6|SISEVs2n!dfEubgXt'
    '^(HmNxyreb+!g^p8u80&Ww}}?ir9L!5(8(jdZUyjex%Oz*mJz9^JxYhxl2b!-@kwU<^Sos_31y~x&3dqe|P({yM1?W|L%+5-nx5R'
    'yQzU&|Jh+~Ju|}xKbDuOm34g~UoCivqp3=@>@$`o{`t#}j%uj{&g+SUdA^5s9X(x|@w#M`*7yZI`T)y0)nhsHY)1Gq=qFAxlHMhw'
    'w4=}N;T1@xx51(w4T{{7zFrpP1X^mHo&WOH=bwq}yzy$YS~%$J)VKr56XPbPjPbHQe_Smu2!E)0zGBQSR!f;`VX}I@q5JZf9&c;&'
    'hi3GtNFDy*^@)Rdu;`byjZ-59C5ahZdw9xfCL-IQiyBD+MgG*>-@w;eFd?FIMrlT0rMIkL=HZ4BRdkq>84+L8=Yk@;AeOhmCZo7z'
    'yj(XU<7jV`=S1QJs#|-$K%>d2u^xIDniHXbo;#9*Pmtt_Npmw`q~`Q|0=zaRUl}hanN?zC9xP!}tHnv!%a7$0rgpS3kNxI(@CG{5'
    'aOeKoMoH2Xs}`P%^uF-Ft|fEapJWk^kr5Uq^=P<SD1k)bTQUx}L~vSEo{7_AUSVhF58f$JE{YHyOp_MM^|B(muwZD1%T+B#`QJjA'
    '{>AECJzXwWFaE#Y<ubBi#fo`p;JiTj1Uxaf0#H-dFkTU;vlfN+Sb;d37gHf7%365Sl0%DTC??8J;a2qd9gISqrE1WVxjj6grWm8M'
    'vq5-MQBc+_Lh&rBJ_P(0xinECUke6l{rs9G43)DQ^*}(@mtGf1Z|;;dyEI{xf-F5XcVZXbPQ+%#{y6t&n4qW28eE@VFgJbZ+v|Mh'
    '9N(dv`oRV{RH?kI(RyeK8qE_igUI&0TNlqa$6caSBqk^J%)?EW%iNlPDd|T(V|x;EK3}OVEgRdf(a$`eGB={Gy1GER`pSl~@s)E3'
    '3EUI8(`q3D>xL{@W<A~AFz(Jyascvy*bA7?Uy4u~Lz2X&LgWlik6HI>!59fJxdsw>Ce{kDtma);>s;RDutMyR#!ogoyN5NCX=mmj'
    '3(E{G4*=G~FYs3goIOC@<S!;-F9qLLt;~uv+~T6N`gx3CZoQ}lBhYz>#TY-OULC#ghVZwc6Pq`*3`q}ffn)afFfD3AbcJKkLYQK0'
    '<++)k#aFV`U_hjyHc>cs$Q!I8Z{b_07K>tffg(bB3%*^fPNE}Q5?<-(n=FZ^33N1eeMCM>B}(Cx9>N=8@Vx*xGfImhw8!(B`3aGO'
    '6lX;4So&zci0o8LbJfx-kY~{bPidzFYDQt={AJC%rl0Jy)2|i-;LtUui7BW(tw@uXR6g4ifkm2Q(}y4q&4W+kfC9>RO5KaVGJ`HR'
    'pjsdFbmMRKBU00pMtyh#;y?_5x=E)&6IBaY3<y!yl@1-u{0aLK`HeIz6cC|#u|I31ix8Z0JyE}ydRb@WxjyOVkO+ChTY}cQV8NR+'
    'uuY#0VH%N$kPXd-h&=g0`Qf!<wHQ|;OLKsrAP|vzZf=c3>GioRiPv{7{4pATYupS5^dmU;xH++JY?ds)t;e^~xZ^tL%ov-#zgE)1'
    'e+)Jmr9)BNL<4hU8*oa*OWYDxQLW722{-8Yu*<Fsuq&u6Q40?t^#V%}{uAngxn#)22f1oug&98~tCLS)Q6jQ$#$K4aVVXC8!lN-K'
    'r5SwKrLp0_eZW04DBvf|7z<m_%)x*P`<gB8lQ&{n$ncIgh?+c*%k$kMx9bzl_3g;$pKc9&GwA-a`=NW-<I;YnnfVMi6{({1C`Csj'
    'I8qv7xc{LX!VXPbTFU|KO;m4>`!62!w#X%7?HEU*Bk7u6;gzP(uk#D@JSkGE#!k`dDcN<S&Mxq;dM2&l`|s5%n-+PkHSO0ds$8{A'
    'fQ*z3%9Am}za^y@hF$Ax8JE6Hu80Vh@S7s|s|_BeHlH=(c?0d@TcqHSd2Aj5#z-C+m1!4g*K$!ceG#uM?r>99c-Ag}GV?seIzyr1'
    'S5Rt^FT)J=<uybPJ(AXwr*PdmT+hmqMFu(5{e7N@o0+*Y_&)P|6}YxqNQ=`$7vP!>&tCA?QpYYLP&N_1j3}PydxzrmwZ3p>PR}D6'
    'h!{n~U)5eNkS*rOC~tIk|LNaEKB{LotEY=b@f_SYH!@5P8Sv@^CMRDyzHL6Oz|qxqqa5%oG8s!D^h4c21kjb4&r@tk9m7!dSxP&8'
    '#ePuC#NZ;uT3lp-wjv_2K&yMXh-Jda^wP9g*+7T-=tLpf>)s3nvg8qEKpP>RUSG&yN_@sEGTCHae>Pn$0KaV4W>!cV8<6Ra9+|Bc'
    'x0vZ^L_3b^aK<%n*jV-n|0|Iw05}_4UbqNxdQKCNwgC>n`Nj!`gpSb?K*+8Z#GAz;SOV(7auY(2AXUo~bT7|hKG%~c9UYyW|K$Gm'
    'gRyT$Jar`w3F7p+Lbnp9koSmB8s#1Sinz;q4|PJ+XM9AI*g~WT96+_mvzIdV=VX|{05VNj3t(Lb&)TEHZ*#_>-Rt-ClVyQ0xE^#A'
    '*&|>gFdbc>$;MQG!Hz_aPoc(2;q9zAeNEt}?1ism3=FLSU}E)U$%%b$Ll^%xA`sA1ZQ%gMLJszIN&5UFakg7GszT9-PZFN1&G@9l'
    'jIuODY)>SnfAB`$FoWF|IWH#3<jd95%^$o{y#8%yHhYCe(qv}9e2V9xWAEZK?CwD1!k9#l@9J|EMmg-)!Cw-2A`h_7pV`Myb|pDZ'
    '{`B%IMZ5)O<DuaO0O26a-tOKTMi`MdjMV~hh`?(=4?B%2yUU&G3p3`)Ych>;LK?`N>Gd0_X}BBwZ4mH@@f@}JoJ86wfDMs-K@nC$'
    'Y!D>&SHTa+olxdn1%oRnnRo@oP~;Y7h+NsRT3(Xc8>dAUH%0D+c}PCPzA*iT7kuVSMEDDELQDXm%=2k-oyEy&>6i_c%?imCj0lJf'
    'SWG_-li#!r$hZtTOrlr@cH1LWUpqC&Iaci;x;3|_1X%i15?X52JRv#}1ckTEmJ47{`Rz_o-%FAe_+NvJ1}y|0$fb?73@_RAMrAng'
    '>e&o?<z|~QgS&cYnymv)MnqB+6FIXa+zeG=lTJiPBjV%SnN_Z?MZkS$BC_hT#}T8ng%2D}j8YlkkB4VMh#9%8k;uB3RC{Z{Hey<Q'
    '4P#r9vKZ3PG7*E;TP^Lc*h!LJt-N6Q0zDE;f;*M+Qa(q*UrU;t_Y!`SRI(zmbd+tg@0H;WLvaH&)CKosK5l;p%Qa7qSb-?8%V5A^'
    '08_e*^+T8rhiQE|CMu`3r86ktU%g5EXJ3{Iy8dl3y-kg0iK~jGp%jueo=HRW_qq6aPY)umhKLLP34hoqro~B)YA<rYnm=Y#avD_&'
    '>=CP4oCf+}Niww}vQGN2yl3t`h0g^c&B09H55k=g6Q#@)u~Jp+I00&E9Gn@ch~<blAX%I-$Mev8Vx>r>ND*TRBtnV~4%Vl~dK3cB'
    'asYwjYM{7KAm|$(N6fTyPno-P8yw=<`_*IVB)H}nies;ZA2^s$)qpg7+48L_GRtw`6)Bh7h_X{8&e|e2AtxE=q?^2$=rVMYHIx@<'
    'y7~xjCXcCN9k*NpM^{B~aZ02QIe3vnZQ~>?YnRQ)JtQruU?wuXI>J%W+18ndvv6(AyF^qH0F3-X{xiw;sX$5olmr;?+g-VGRnni5'
    'Jxd2imOOt<(#t+?@Mw|;$U!NbNW#$zA)c@sbQOwU1SBfJdvZvij7tVhbE*RMmRZ?aZT^t*&v=F!J64zm;yZ0vq0P%s>Rr!KIb}s$'
    'C5XK?#X+^V7v{*;$l#Zjvb-%5Myl@dz#PmG+VrP>5jY|y*@B(1<%8d*+D$$KN^46=U}3{|L}3D1K;i{^OOOw17KKer%k?5qC+FAm'
    'F=9O{TOla`iRX{)Gk_d6G;0Q!J+zt^OsY~}4`MPv86_feHEJF}=pz-x>J>gjm<)Ra%RyYM()V1XV+5p&iCS_Iu-=7SMxPAS77@;}'
    '=xKz5TxjQ}JOT0(e&r%VgdHH)2_7~~UiRW|SR|FHxBx`8k%q}(rW`&U%d&CGl9Z9$uNLFx3qPDDB`b*&5^r)17D)L~10ad8jadve'
    '^8pchNmciJ%=9JoRiEzNqT15lQ+@W3(5IqeoZ$y4=F%a+5^4JEA!xWLRulswwXTel7fqpGfd{G>6kn3gvNx1QP+OYP$6u(#@@(Gn'
    '>n<R;>&Wj29NF^C26x1f>|GO3u2GP`AkbeVcgRHO2POD?ga87Pa}^^iLx!A$6H{fj(jFP;Qv`{mn&Tv|nZ?5~lkAs+H^-nCen*x{'
    '{AJwMF6*_8UoB+B>yoHE7m;(WYg_z4M#K}Tlv;*jx%ERk1`O9}b%=B&3K3Q+V!1*+8rGLX=E<5_*j3vU;T`j60xX0yNZU$lM)nkb'
    '7AnFRO8Az^n4wjnXGqnQ!sWtx{F&q-ReSYF1mia+2xtIVe@L=!m-V;f?B*O;*;D}_>~D?Mt}ln_#=aL2^AaTj-rwTvT#+Mrg+bs~'
    'jF*%6T3a}l^{Hs>S%_dXW}4qp8)L+!J&wTUDpj~GJyA(TddaNhILw;4qM1kP6=wxE^ApuFMFiU6%q>y!xtXDDf|YeLP9F0l4^rWn'
    'R6LrRMqon@4=kidGFH%0u|uwc>F2Tm8BKA9)`9@LJ;}<zY9Pj6Rf}nfYcJFQpho15t>g#b0`ybpjHGHosyQ=2U|0v;t0kutoktUh'
    '_Vr*Bt}RZnE*ZP?6fgpQV{00@4eL_f&5zrIHz%pwfJQsn=SnqG&OwmIC;JU4)K{DZa~M@qu2P7BbwvDpMx>WTW>OC=NRk42X@j+Z'
    'MO-h?*TJ-67PsIOwSaZbJIY2Za(nTJvrnZf%Rch8oSqjZt;$c!p=zaRF_nkpcr*$doO8&83>IkQ=A~|3z%#kdqTP$K;V5%!IeuZY'
    'r@Q;-&``qhKx5f78gUR+yY-A6Wz`88F;gr<lgO>qlAF>_Vl2yQdEFvd5yNbe;E?hdXv|s`Dr+r@uykaNQJcuDQ_U5zg+#cDJj~ee'
    '+EBQyvZl$b;xhF$R(X-e;&V#-8cPg3l-4|!zE`Lg4vo?bY?fO?->7g94eHTM?ddk#Z-XR(sSAg+Lv1VL$uvHw7d9LZl}m{{ub$`X'
    'F2$#CF-tWYHrA77Zd>|la${_sb}MElZ+UF69A5Dwctv~JkW_K!j2vue*M^^w(nKK8g#LPuD~ViHtQIK~H!H7<trA*8$wUF_Tf~ZG'
    'Okaaa@MlVLO7t9*;_ur0wjhiVg!O}G<PK$^)1WSzh&`<WN~Cu%TeaZ3gYI|kP&vGK&H#@Kn83}n+Iq#YAqz;y^p$Z(MozjXlOTF-'
    '!v=8dq+NCN%6lC^cgp9U9$yH|mMcOh^(1*WMw{d*BD1gtcEQBVVt~U0*0DgSi!Gt$sv66(7d1*QdS0%~@2K*tBLDx^`2V-Y|36>j'
    'XZ`JcdyO5CnK(uefI^8;I^-%#9VJoG_Ig+MhaV6e?CQP&V6O2&9Xi~QI;gG8Rq^3<#q!bSdD^UOD!zriWZcCqnm{I&n1H<~Xrc{d'
    't`;QMq&tbLK_qIT(+*N~G(P9xQf4pH0uc((ArPtYHX{OxAdD%+m)H|g1hEbHn-9CYtCdAOk=&&en`%NbP2%C8Kp%aKxI=D`(+>I4'
    'h|?*EpIZuNB(m^HcA8;IAKmCCpEAGBI%gDjMTO*`v9Ynb(8fhQCRtj{{>=!Vf=a5H7Ll#a=|_`#VW+3}V@O=(C*c@}#9A<h;AB=R'
    'ytW>7ss>C(q%_aw@Jz}3^tD(Ca-8RL7Ic6npESxl2<g}5ikL1gUswx7ZX6|ZMzG=o2qK>mr5Q_@D*uBo0^D`UP`^fU0ACP;!(JC1'
    '(MX(-3+?S<rBYQV<TSCMA%)EP$|XomRU&0&PV%rw(mdIq3W;n-M6mp=*S}zN;-aHfl^GgZN}0}Al;u&1a<R5}!YMRF$Xa9uU;lm2'
    '&q*$Nq(ab+b=T5bnI^GN5gsWchq9)o>3jlbXqHt57HRLJvAaryxqwUM-aVjR3?<gq^qH8l-1>Hp9-b6|=nvjW%+frRVw-eEY$~em'
    'DaWz;5|6KU*>Q=r9Y={(WP`!IX3V`rWWA7cVY7x{DD_F?pXmVp*|5IqGhXINi}#ZDiLbK`LSZS>DF#@_{;m^=;a&oYrL?-P&+ub('
    '&WNaAFVi6=If&^?B2?InR4S*MMiyvTMOir^rXC&~X@-;*4=Qrcu`;8S04IK7q|ZQyH88E#5<c%MaaKo1SIH)hM0}F6if!5i(f6nb'
    '!)246UUgnfj*h%SpGZ(~W8Y7%WZz4s7n+xd06tizSCm;RfkekmU)1g>$w88%ym2W<DI%*B1#)RP3Yo*~U{Laj(j=H)95M;xS6*qX'
    'f;<ZCx8ZYP*^IA}{w+;Vm0~@JEKoo4$&CVl;ryxa@56jtNC-)Jn#QEH?*(`mmzX*gGhnD><;7l91-zLeW4BHYxfEQ77J&_xV5xGK'
    '6)8#~X!9nLk%zY~Fbkfr#y8foiKBcbluRNNfrDeb10UlwGr1MO?5CrRl+tGAT<mYKdVK{ppTdHbr{xo{(4g~_W)xo|`Z{Ew`p7*B'
    'WAO~RCPSgx^8gGL`U4+L9wmH4wQuRK?bC2z<&#cgdYd;Exq&*XGri5z^WLJEn&5&EBXI7;*?&W?5FN-8p*^&RwZgGl_*kYFvJFQ$'
    'Z`pdHwss^*oMeF8wPi2)6kd+33e-1VC*@(gaZ8;P0;LqX!Kd@709z<*^7`i4nL4%Uu%nOvzUyPGg*r)#A~f^rmE6;hQpK+k(lR8f'
    'mz_X;{G5&^L;=6U43w}cJqr;6Kod6^j>>{$sVEZ!=SGxEq_@od4Hq+)T_#sZcn40J5Etl9z)BUeopvgzQ75M)cUfW*$a_I0{hi1o'
    '{UdB9y$c=LB)Aht7=w=X5d0tvxH6^BEpiMYu^k@~XPI89f^rl00$uRwNRgeVZJf$0pkYehAj`Eis!Yn=5yo@Y<`UJTye0E&nvX4$'
    '{Z5A<S4;=PQzkBw>002KGIXFu49OF!0G5a-L9%PLX&3KNT{rT<FMj>$Eh|OgZ8<86#ZTB|Ev3`LrvRfQEi3>rA+EMqpxR&^K{S}P'
    '9HCk`)8o7BYztuvm5Q|@0e~h{dP6zekAO}u;cVH&d6cKyGJ!6s;u^wzIM1O4?iZ?N9&WRF*=57dM5`Avp`VhlI=0S=NjTrQKIs>g'
    '8L1kCwmA<YGb_#2w|K~jE2mHl^z?#32(vvse@C@R+lrYuD;=Ta_Gsec?&}|Qef$xn8-7A16fS%9<xo#ImB~&HIP3VB%u&Q<RdDkk'
    'v-%OLi-sBLaE8rfkS@Jkkp6;bb2+ry#a%hspy0s=Mr6T@kHypZ*4R9QJ>9=Wm80!RRg1M{YU=EXR3~yK@EiDK)!4a`35n5&hg+g7'
    'H&M3oKqty`oz%;6lXBvSES4~Fs;H8pnvpF*qj>MlP!)HPPEytnBxMy6f`<B+=zuPXsYJjia8OH5(%Fu1%qk>+-Mb9Dc{hUBCoA%6'
    'Tyx+6R6jr)ucZP6OcU_;(<+By$vV`IV_Yk@W%seA2^XEWI$SdkUnn(Rns6z<9Y3dvSS=W@?YyGRCnVlTa%q%>Y9-FlhtmZ+?Jl@Z'
    '^1g*}GK~<T(}EREdH<4V;pfFDKPO+Dh=L~GtC8oqQf6=$3zDvHT2h5kwqmt~gW7VimQ0J%akX54+-Ym&BqRJMF82G)Yre26fGXO{'
    'wq*%++UyX6BLU$+Ac*pk)r>3M#O7rmRlJaP(9zULopPxqjmUy+PHikjyfVLDHIGtCR}yn{!6Ee=Vzn4TWnN(H@;Et)(vGce)caG^'
    'jX3LRPIoDAIJ47G%8`YV?n>2ZpQ_2?h-7cu%u1$JZ%(La_VkuGrTb#!L}ca${|SV7QE+MrAth`iC2WLsmJnrvoT%+axf*W?QS3e='
    '+9Xb-DHxmCQJJi*N*GgLEb)o<)FhN4209Rm6Pu_yS6J4$91!AH>@?Y7v)a&zmV1V8u6tqSf2GM;=4M|Y%!h18S_U+YFoQ2?({?WA'
    'bf|k%g<^9#hmTw8*|a)NFE5+b&o)ny-ddIUcC{d>rBn}k!uRm_?%D1C_~Q1LcW!_7>FxGc&x{-bML-?eJ3XX*qEJ9zof3=N9cnsm'
    'axoEILaR%qHha!No9}j5jlTc7fB3(@!T*m(`W}sr+;@+S4UP==4~;#cTN~=wk1t9aBV5RKbpP*f28Z3l559g3GwUN8L&xh~8edzi'
    'Umr<;EY3PEI*z)>(C6P_r?>y|$UQnp*9C744c!^OKQj8T-vc6^$OH4|)2tgUdHKD=&YxWJCf@tpvSW64cD7p@-3{|~b|MH@PiZgB'
    '%+E@(%<+wccD(59q+1$^NN498mfG2QO+ySc{976)@IYn++N4N~{Swfsb*b;-E8}O`9@Nj98{2$!MBRw0A3@}W@y?lk9G0{+3tqw1'
    'gLz_0XpT?nbavVv)A6yxJV73tlJXuSZK5rQ0SZJq#uHtF28Zsx&-m|iTj{+nD}DcX%stx2*Jz~y@KsET;I_LSbaqDG0c|+SUBB-l'
    '0x|X0rmy@~X`sBk(|FuF-q84sr^4G0QZ#mos+VoFi(Vc76W3PW?hsnvWO;kPmX}*tecpJvrLJMJ_rw0-{s+>@&{V&7_m7S_?z*tG'
    'D1&HJQT8zySaT8Za%sQRT-Up-kKOcv#p4?DNSc2=IO-nojEuILsKGGab&3w>otipidOIJrrdu|OiPze(he^5Cc2)eILt)3jQ2*GN'
    '#&_lGcfqeX?uK9eq(j3Wbl{uZ_6zlVGyKfYSKayAmMXQ=_|)%b+Fz4i{HK{ksA7fg(dPr|8MpXH*f3V_`A&Yj=f1BId-?`O?zyjw'
    'xrgpw)js$KZOk*ubkLtsx99OFcrzdgcfD~K;@(?^;dMFbrA^(vr&J_d@ix#)LqBkP`UcdU`o71bLt__HG+wp8n5}-pp3kH3oc1}$'
    'V9)&8zx4mPUo&3j`N)Hl*2clFSg+|1VTr*ZcOPlhkmsiDCjz=8x%~al{X>u4R}|~4HITwI_Qw-2>#8;~3ejI{m}qd=;~xF<p!?hR'
    'p2|7G8z%d&$L=xG<M*D+F+iKCt~#V|ELMQKL~ORD=rR1rx9)owQkqI1X_`$1#@wD}xzo-4Tw2)>ppWW%7hN^sEjuOsefKp>GoLt#'
    'lQ${(2G3he32Oyb^nBBIZxEh>fToSkp}{fFAJF2D&D5%2Z9qtkobz;~pH063QKy7qIUP3<iE-2W)VZ2Jxafe?!91rtBo+mY_s0>i'
    '={pyu8LY|gQNTGLSB2?2C%1fwTE0K`ipV}^eXc*YTmHB1{yz-^V87q;MdZa#w)|2F!HRcjh<sisa0WO)-_=E(DAmaLJ6|7;=?ihW'
    '+^0JtJz?hdL}ga8FLx}8S-t=-$CHT&ABLCg3hp8?HV@ZoE6?lfscWyr3uQbGj@%m@@U&;iR2<HU*R8KS1^#PVYlHcIZ-p;DHiX0;'
    '2$U%N);;*(8@eXi-PP0m<XW3vyeYxu?92DEpBTA(Fa2Px=3U}q+~s>I$;mSFyY$MKd)r1SU<ME0)XO2d`=cYnf1?1yF-w4^Z%Isr'
    'E-OHhnAW2iRYQ_Td>aVA{stk+I-~uU4KCZ0Z)jAD#oD5uE?G=P8+!J(?^&l--k)!#wzmzFAND`$8ygrIb@zEjoZM#DaZRJh;vT-o'
    '?Z!UQh~JX?^r0Q!mB}oui@gcn!f5jN4J&EI?oS+3Qvkf;RKV`8?yHSVX~qB6LpkiG#ZWB-(t0R|F|`<~g@oEIu)$Uuhk9}$J4F3~'
    '$Mn-lefI~4`-gapx5@1O&`5t<q0z5UQ2Fc?aK6=K@V+)Y;&G1Hur?)8P-;D9UE;>tO^vAPdb6@MPH@#4$ur{VAHom~qXW(i+<yRQ'
    '#v{MMS6po*74IcR#()^$4{SewY-qwpQ*WS5sIiHp5iRVil)|eI&thxn!|V9PP1U(myP_ugrPkAJYikoGW*8lLJbdqp?0Ze?z3Mcg'
    'hy8!Kf|;&q4+n>@*m_>0F+o8S&f@}J;Xq%TX@YuyPWRlSeVnHCjUkT$AC*F>hg4X!X;VXf;cx(4P2{Xs-qE4stP)1ZpPPx&4#eg-'
    '9XyW*<S#>Qh*Xwwf{~>S3A--YfANbda(!+qgY3KK_JGv-Pk*&sgp+NpS87}1)eEjX8g&C_4G^kw%&b@=pHd|HM8+d4FtrUywmMzy'
    '`2?T4;L1mxQN*A3TYh_LojiEMx~vs7sgL~O8ZM<7`=$EtrnSl$<QV-Ig;Q0!Qaoclg8E$3Y;~<qI9GYIV%ck#+9O2(@^bZ-Ie4~3'
    'zhER)$~xf9byMbhhb@c50DT_yD|E29=AWkPA_-VcuC)I(x2w%OZWz6b3msQz^*8CShSlG+eZJa)nKqd#URu*hrrIWdTb+ejZ*^!_'
    'p?5TEuEaYUw3^u2#!*jqymcxAzqy=BaH*)BO6c?JdiDjKkg%EG->wENHdZdH1&$E<HksXgdy_`Q(N3y2+r4P)-Yo5#&5s(pIAnX%'
    '@x8He8_jR3QPgd~eedyrTxFvtkzM}=ccRZOn&Sto4lQ)2nAB5;>`pjT@y<#+=d6C_vWp8dT`aP?Z@&HOfBp2<-8-Lu*7wDipa1TQ'
    'yG}iEzOO5awZxm&r^`ByM<YXnp1}cv1)hG-<FTKZcgb`8phUk;b`Vv(doRX(@%eA>+`aSZt>5;2b@$fYfBmWrf7&$r3D`uYIWpQm'
    '`dwc?jN!rXPa3fJWGQd6v<+B=82{Fso=vMC4tpO)k_-C$%UfUmzVFsoU)}l5XEHjqX6753zH^weRqEu=eaOp`Z@~vvrLKX^?+i1!'
    'WXFi4nYs3Y9{t#${Ix^gLvo<eOS0Qdg;(CHP%Lk;7ALD)Ew?q+l(D{E!~5G9!^OOIjIo#c{y#Ftv6fbhaW1z#V=huVN^u5<AGC%+'
    '&8)hnU8I?e(XZKx`p6SK6s$fQ8FpU^ZKz_B%$48G>nXbJ9BQ|yR;+5v0NaY~Yi+Sgr@`_sOTNKWzkWP8bg%DXoci`cgL=M=V>}BV'
    '(r;m;Hn7fdSRM?MRUUQR#ixGKOx8s&$!v?}I=kPt6er8yy7a5<%J3I`#=aXvY<YKHY25MM)T?R5zxltn`o8-8S8(-hf<SYFM)gp~'
    'k!2F!(u*t2yFcg#tMIV@@sP*1<?zM|p{+nrD=nD0bgONl#u}Pcx?bKj)-uAoD1Wf1=-5MZSy0i|R)RNY!*A_7$=%Mk{f{0E4Z81r'
    'F)}t-AB?Y3@n#MW?6TM9{vS|F0|XQR000O809*`R8X#i@4I%&l7g+!R9smFUX>Mj^a%FB~WiNAda$$63XL&DaZggdGZeeULaCz-q'
    'S#ulLm44^1=t3%05<}A@WjT&hni`d(Ni>t#9$QH!l}WK^3X)l&NDT?aaa^hbC}JfRQUq6Ul>lxM08-)tVj=%ajN9FK@?V(m+;h=v'
    'GypA*D^GFRG|+vQbI<ynbMJ-A<?0Xtzdn`vt{l2K;&G3S3=Dr)4jb8dV=g0dYesy(qr*J+8QUc@xF;&JA{^7hUVSC34XNguvH9F6'
    '&l?-Qnzv-0&3sp0?sx}p@3_a@BZKaD?~je%yYCtAzW=pWEo6=5Nj))bq>E-gCJKdGFi|bU^|eV+DvDr5pWAQf`_<^!pZnc^z3(0y'
    'qVN5;-5&R#=VB-$=N0jQUU;Fe&wlIm8Kpx}+~Q&J)yz+dP(WYY5c`uDRuS^9W~6eWGFL535ivWP`sz9Uiq!?Q85|wCGjz9mXk<wH'
    'V|{`Bg9F3EUknWX`A?cit(p6e#LB){^>(<xypB5<R2v=`xjXpfz|csynnN49cYk!uqkZ({fag>B=$8B5{ow(RyQ4$Bdf*uv?&j(G'
    '>9aO4uGzlwz!oRE$30`{efR6`F9ycl*6@n3k7U0t?KAh_*n=U@=c;W-$B(qXC%yPrV<A@yvqo}9V~R9hx@ytU(f|8TKKrl!>mU8<'
    '=8fOo_|1)vZ}s20@taS7ef`!A?V5(Q{%?o5{n!k@fPAZ!4Sg|REqJSiJ&~$Z%RX@OSD$rsR7)ih+0+vW^L(H5ptoByUY3l~I=^7m'
    '&^J!?Sk63~75)tRiIYsVvQjN98>L;yAFsYEN9k>_s7He$x2$iJMLB_%T4(2fe*VeFBD-Kb->Mc~barao;al$njSIb+DC-Ny)$*e7'
    'hpOkR#@tf1l!2hD=bO4OkLmHYws2@hABof<G(@~us8(X{);3Q`C#GY@_CB8K8wGLxNJMr(7d4Uuiu|d0u!*nGEfJkJO0)VJz10`v'
    '#%9hu+%%$!4s$9a;_Lc+P-GXy$`06M6t|6M8)jsJbhJD#5-0jbu=Zq;Mw3%xJ@ha%Px_iWl7mlz4WgJdw*p3LUeCi$_Kd0L#<MAA'
    '74((VSCU#SPQg~cE2l8Eqm6m&H_wAt(V2!j_t!Q{lBQU-@I<5!g#TqNndAN>3s`qz8Vu8;;cB4-5`}NsINTP&8BuvGPLFwoot@u$'
    'r$xCaLU=GkUO}$6S~-V$xv*ephs#whM*07RF#R8^bM<z+Tz&ZOO1I0%h7~IojzOAQpnL+JnA?GBp~xD>D*|=aqR<{I5N8WwI>ba-'
    '3vXI-Xvqx4MEQ}v;?);+F$#5-szFcY_wj_9VvNqt2H{OdL0Pj1#p9^@5b#^%(nN`TEf}Qr^J|tcRL*MD3jtYQ`dlQv`BT#D(xg!e'
    'vh>v4i9L8&5t|bS6WpU=g5GXxaD8Uc-14DspYxe>e1~r8FE%j+D=%xb9-4+m^F+)bvOVwi#q+HR7u+;4Ik9gZZn<3M_9RS6Kk^wn'
    'Q;_rdYHfMN*m;S5=J~X_8Fkgw1=7`5H<gXAo<m6Bp2(e63mI59WXUq??dgGWcXpBkkPpOuz<lydgwhz2Bt9J?XK;GVx>pOvNPx+8'
    'kjOK!R)A$S?}l3E${uO-S&G;pjh}3GZl5EB$jn0)mKj<e0IY{!;I9xkdw{yhUrfYa3cjscnG<Qa#U*L=3lw{{pVopA=sd(?Oq^1$'
    'jy`z9<c=OXv3Wzwko532IA(tj)1oFsS2zYOgelfmpP2bMd?i~A21FWa6RoXyA#bpXyoGPES}cm0MGPd;+Z5N<rqGcs39oeYO_s!?'
    '1UedfJ|dr`5)Fb<dI)cX!S@5)%qT61&_2&g@iYf1&WhZz^wEA1*{zo5tEJ~4&!P>U(oPA~jKakEvzm8ZKRIBhUo8f}p)RJ0DX2ZF'
    'NRyXTKHe9BC7NT)ryoo}^Wc*>pnx)-QuiXT%%aOxsMb5ZJ^0IhL~5GSsQ0cy9Ebr>H|aEJB0vWW2vOFR4js(=3HuWHjWjG25TONe'
    'FbAjt!70}h^^56ebw-}+Q+^JKkT<*~Xsrttyg38g^tlkG5s3)d&|HYflOL2HUN2UQaW%3u2M7uR5xFPk_5_q(pU;wbedod-qw%*V'
    '%wRx2f^(0XlLy9D$@1HJd<Ts?u7J*rvE}=FB`y5NU=sr(G%z=J0Odry#4TYJ)ygcMaD$Hby6w6EyMoFRweS#9FR%pRKcOy|ONLy0'
    'kgFzEnDG;`I{5?^B_aoA?5VjIrg`%xJQ{OSn!$VB8XFGW2i!A*0)E1bv9JZr91N(iui4^0c_WsE4DWb@sL2DlJYU~;yFS!hgdMLB'
    'emUg+i~F8?#N*O_q?!2)Hx;R(^e9C~BREnTVr1Z+9KsGwTw2Qk>`hc}kNdA4^tQ+)V(l15q9f^=p5v9KFKqA&^E?TBY3vrQo|0Wx'
    '>+Ayms%O#)zWGM2vT2cBt!eMFsB+b|05VcCC{M)<|F)E37<R3%XI%OUxgsK1LVt?j&o_CP+CtWd=MA)rZ<B&U=CK6?7$bRPRAyYH'
    'T`NV^^l7}dw98Fd;aR%?%FOc=>kNg4pF^odz6>+eSJn|d^hjDyp2BtOa6PNb78&GJ4-R-DZf54r;QP$;HKUZR7SiJM&;_`r!?PFs'
    'wbZeT2$W5PFC&U4`u?FfeW@>=nKKKB1|mk$@aMH>i)4#AGRmtxJ%9e4$Vc_;R`qnrD4v7+=4OV;Ap>5U#N^~lCw9z76*#)uUX%l#'
    'MJ8h@gnp<yhyc1W^LdIbsbd(bKIf-N*$;}@7+j=SkBcnORzxHgX?4$*uuK@4UYZfBo9Iv<ohU?m-J796mOKL6_014ZuP<gWB|hU7'
    'nQStzKc1-;fM0fMv#TVHO~`atkIYqz+syPdq8&$dIOCc(Y^?Z%|G7vM0Gy3&FI<E;J*Nps+W-gPeB%T|LdR$cAY@ky;>}VKECKak'
    'xe1|1kRIj<x|ioLpX=enj*iaG|8Rfx&iI$3p1Kl;1aW#pp<9Vl$a}=6jPfpjMcie*hdLqZGd?0pY#~wvUO=_TvzIdV=VX|{01{bP'
    '3t(Lb&)TcPZ*#_>-5&4^kY#}|xE^#A*&|>gFdbc>$;Nbm!Hz_akD$g%;q9zAeM#V_?1ism49s~gt1nAV9C#bL_;(P2fSziLFJLU>'
    'U|*J`&p#4pdv&8K6pi>4;knwZPddyfOGCu=MPlY#Z{!s-*lm#}M0N(r<jd95t#7?jy#5_%HhYCe(qv}9e2V9xV{hX#?CwD1!k9#l'
    '@9FauMmg-)!Cw-2A`h_7pV`Myb|pDZ{`AdPig*jm#zVsm0K!3-eLa0wjW8l_7^?;15P_F~9(Ed4c9%QV7iZ0rmt-2{gfx(O)9W`<'
    'GjKQfYY^~>@f@{<oJ86wfDMs-N)c8<Y!D<4*1!+Qolxd{1%oRnnRpJxP~;Y7iCo#ST3(jg8>dAUw?yu#c}PCPzA*i%7kuVSMEDDE'
    'LQDXm%<~y?oyDnY>6i_c%?imCj0lJfSWG_-li#!r$hZtTOrltMPUQ@!`ueFk!Le!=(XF{XCBV|BlF(9X<_Xb>ASk?Lwp;*v%5Qgz'
    '`hJqE!2c3tG-x64KrU^pXL!k`H!8z{SI=hHD>vJe8QjxDGi)7jG9r?qn8=wW;by1`n{*;V8WEr1&a85EEduU46OlESJ&qWqZG7Np'
    'VwB1Ne>^-BLd?izjYQVPq}p2pwh`0f>loXTl*N#SmWdd&-fC%o#ZHp+YV|407wC~-65Oejm-0Ch{#w%HyqEBsq>>ehrK4<{eXk66'
    '7>XOHp)R;5^Ktt-Sgv_;#0o@#T?PXV1DMifY#hRLI85s+F;O|KEuTRF|L#rVU;DCD(Dm<#nH_38M_g4b4W*E*@k|<`zt6?bdwUUi'
    'HAGzSPx!+|F)dDVRC|#F*8DM}lGCVKV2@bU;tbFSOOmM-kqy#^m3^wum5LywIhg4OLAVoQqLi5;R;r2}Cs1AS#hH<cSdNGTlEoQw'
    'A`iVMR*F=L6fu@SBBbcxV10V5M<MVm2M{=}28s&>g1+$y#7sN)l(|c{!6BZ#Up<yifoqPTIQCljfrANE4M@XREZ?dkvm6Irk#f1s'
    'C_6>stSw>_a*}~gy2*=)-h@uFhVmj!S0CZk<S|vO<CaU{=&A@VPK)#*2QPA{ZJdN<?XnrUhomJH%tWSFM>r}v+dA`b7Ot&%mx)RO'
    'fRUfb|4p%dDo~O?B>@Kfc2}-kmGq}%&(gt>B`+M4^s-MHJeuSIa!?8<l5q4wh$rj@U4`No0f`Fmo*WV=<B~zsoUTB<WmdLcTR5cr'
    'GoGQwjuobX_)Z&EX!A0Zde?JQPFWFG31Y8Lb5QN;gE_J_GWeyXtnA2yk*a$<Fb8vlHvN%b1dfPFwqR#$`)IRZKY(-zl-8D$z`};_'
    'h{6Q2fW%YwmLMP2EDBqgmg_~JPR_6AW5jw^wn9<>5-%LvX8<{DXx0ocduTN;m{g^{9>ipTGD<|`YScV{&_^nW)hm36Fd6m;mV>xh'
    'rSFMI#|TIl6Sd?LV7&{uj6M~pEg_s`(bEW1H!&UOraS@i6Mp3)Lxde5*9jgrOkVclFD#PER9pa}+DOCXFjEenj%C?6Wl73N9#o5Q'
    '^Qj+BlaiIh35hqk4hy9Gr~!~f*v2e|n)!eTJ)^37K4$uo`kGJoZc}Y(|B*g-Na#~hF~RVI6m$6yV2Lz+?hrIw6f24Wk=jtk$&04Y'
    'ufPLU42sW4XW1J{Bd9G;>l05^VtKY;`E?f%+!f?^1deR^W`jH8NcOe~DAy>+Ul8aok~?G~^cN-ge1rf3lJgZKD?^5ygcDO`w$dIM'
    '=u-rVq?+R-ubIWeF_Y|<gEz;Zr+!D4O8jNq)-LO{jbANf#LJSXJQ0y|u4`NTKt{w9sgzoVV)z-}F<`h(t3#wKQHZco5z7_o(XhS}'
    'GEdgc!k*fu2=AIllVBmFLE2VYH?pVjvrrMnP{OxD#tf|rJ*Is<R=8YPk3W_?q-w7oiD3NZ1OW{o>-R|3?Xv!MoZXrSE1N0+g#E42'
    '+V$ly-Prd6V!lC%fcLjJJ6GgLUSSaU730|yzSb6xWqm4IdmJJdjhPm<)y5ccX`dsoxkeRkOHWi%kzO_{IS#XCu4v|wdc|46&HQAw'
    'OcBA@%mI*4^0}3vZGzPeGEN@zBo9*Im{dHPo<U$k4i7A(M>1B>QL#g=g6Ze70U1qkmezs*yEDbgz-k~So>z-$iEA&^0H8+Xj;-Ve'
    '-~#kh=!~RlL8>`3Kwwx0-K!<16`e<ui1zhh6Rs^yvo0BX3luN{eq(zExee=5-OW$fgEuFs+<-<qIp9h)Q_ew<#wYs?Db!b-19KQv'
    'Q?62ofptXuLPn%lL}p44ElQFCdTEojfJIy{(AUAVVivdH6t#eLE;!0YEOLACh_g?nD=R+owVa+8rmV_O%b{we88MxQ<ajg+8=P~<'
    'gbWsF<QAlEUBENB&Z6Cmvf(ImYdL;ltGB1;$Iwv1@jzqQG#YUb<!u#(9c9%C88K5VLzBp@){<M&PGT&}YI(yVSP{c)k>HT>7--CT'
    '7Ak8kim-HKol%>}Y*5V=v4upqhCIyJ^x9Ckt+J-ctl~}TYpn7jjm77b_BECmcqpxTEPcOFEgTx9S=cPMhQ3kZAR5%8ncAZrw%-Ox'
    '0#g?bX@}ZY#*=A$P%msc9x9g-c~U*k)m@5D;bN9*Hf(Gp&D@Uk)#S$5JndG@PTum^U^%?v`|yhPvLUJB?io4Q(4GxHBc+Kzpb7ou'
    'K35XCs#q;jCT>=q8`~wchLVW_)VGKg%b30nmEg~m<do<+D8=8}!j2$}5rp-FXXFlLpwpl(nutBB0!pNJF<Z6ZyNT{MZ&EqDc+LQi'
    '3z)#owAy;bu^|gc$Mn?+M@CM%CzBw0Zo>v}?4(_F^vZi3KzGXLo*rKa%$6%cC-o$GH%6P}DI#;Q26n;3%wmAU1lF-gsEaM3<*FLX'
    'vKKW<E_%LMncq?6S4IAR)%gEa<NxQ^_*sAZUSDGeWG0Rg1fWo2ln%MdQb$Qtw7t^Z^WHlI2fKT&0+?%jP=^jTq+Zlk=d1YevSRsY'
    '^E_=<wiMsOUNY|DHccQCOH9CC6g1HWGFJ<d>(ZUX)gTfz(P;;%IvSsIa4EBwX@Lj@=n#n1c$*OcMG(f6;!Es{D1z7~{LOnkJ=Mw*'
    'o=EOeicK{knI`dYP@wleK-?iW$Z3asX~gLi#E&h7GZI<&Bs<M8rT4G)kWX3IV4X9Hd!j;e(Ad~mU1;N?9+NCBX8&e{PeCQsOpC}?'
    '=k=p0y|CNc_W>lX@{@3kLt-r$LvS)H6<%AfI#mNEBT|}Y^LVD@efm<Y20702ISV>KlTRAuU4-;2az#uRmoKaZA~%kbIU`u{0R)lH'
    'h|;VjOqKt^7Xj|NWT;;!Ie;&S!C|k9j%XxK$c6TGvr?(56LOkZ(2zoAedQ9QrYe!LGADUhBx#;(QiVjeBO+M-*6W`zI&sm_s>&=4'
    'Eu~E7E6VaHMY&j8I^h%=B4jNxgD-#9`(u)e9;pztW8Jm1R;EcTRD?&$$f2yM89JZ98JcC4fhF4eXzZ>MVJ_fOxpyC^7ek5lb$vFb'
    'EVsVhqlc$NAo{I$3bQm1rPwB&5u1vtd&+UFzQp4z-F94JZO2h!71?BPuNm{t5LqweT-d517)pH-`Nukde>SYI`HW|I(&GK3ec~&u'
    'gHTw?bczAiv7dG#G2Bl;v6NQV^%;JQ&KVK)8)Z7gBnL5lNrVcUkxJ!M)5ro1t0*fc#MH}!Bh8S~;z333307v565zy7jPx1kum+~p'
    'TEgdjCC=(FkhF;-5uc*0Vuv<C^nEJAaM`4{Pn{Q&qa&}-ClgfMIPjAzIq;I{g%%_tfDe}G6=l{+Akhia7qvS|a**UGufCC^6p=NG'
    '0=YCCh0I}gF(~;&X%fsY4w;1UE6+7nK^}$n+wi%tV#e1<|CT4IO0f|{7N{Tj<VFF&aQ;;I4`4nnB!r|qO=Hs9_X9kPOH7}NSuj+x'
    '@?tNl0^UrMv0EpHTnes3i@*j;uvEFriWH>~w0RTB$irI~m<3N*;~Q()#8EyIN+uDCz`-%zfsb*TncNCs_S4ZuN@+9mF7`KAy}k;Y'
    'Phr8z)A9*eXwZ2|Gm0+}eH}7TedHd6v2=!9lc7-UNdSfl{eh1rj}ktj+PC!A_Gvh<@<}H#v%?#U+(4bxnc3m#d2dlnPjW$s5jgka'
    '?7yK`hz?|l&_3G3TH#nNd@R!o*@mN>w`@I8TRV~@PBOsl+KQKa3NJ@i1?n5GQ}VFggr!ajfl><H;M4h3fGrd@d42QjOr6?v*wF_+'
    '>;3?1p-$4G2+cfyF8B1KRPk$sv<!*rWhYP{Kc}M!QNZso10}3V&q9O%(8NuKqp~1bD#`@Gxe?_O>2331)5XkXm&p|p-hq=Q#09z&'
    'uu_F=r=3b_)X6EyU6z;x@?KC$e-nA6e}v7XccCMj1b5;HW6;rFf**tdSEu#)C5|B^w&Nq>EYmAhP;TO0pbI`7DY6T+jZ=9JG)&1G'
    'WVyahl}Wif!g$WwT%vlEJo(SZmdSo+LXaz_gW)L?7s+%j@K_l-P$P!q2~_|~M3f-ewc50c_o%KI`QWF&_~^QoqVTpH6~*EwY_gWp'
    '>ETm=QIZxGfS3?hTP#p*u#O-a%vz36Eu87`J$AN*u!Ty+dXWG?6Dqx`obCHSr*GhF*~EF2M>{fsE~(-g!hJZ;p#|<2s%9SUuzA^K'
    '!_Gvj7c!xrlCV0q&Wb5G-?%>I7nND58ickv4<j=x&DXbh$cZbbPz?0;fk6nfy}duB+N5p8Oq`XDP;z@T`9aT>_q#uMpVAFKp%Mz0'
    'efmnMw};APCoeeb_<+n&#O73R^B=SN5vq%Z8R>9_&18@+JzJFif@pI&wA#g8IoY7#!3RcU!HbW@)A`oe0)su>zfF~+ohem|wPkAR'
    '?1@w-awhN__+-`CxsnNq(TIoJqAWL2w(~$I%5$C6%W_k4;)pDkFmbA=lA@ZCEkUDr@6AvZcZp6?)(<3Q6%vAm`j_c|E{Ul`z$oyd'
    'mYkxq9pRW&NC3O{7<lt;1g}q4<kz|8zyYX!fHq!B1qhfX;O}Qt4#Sdls2#_+R&LAgV@s1RI&XEjZXP~WYP>w@QhqyrP8G3QFkahz'
    'PMc3iypiP6D2vrfoS_e=3wGLFaGm6R3ln4-Aw;J|E1L5DCDFpqi&1_~zBmyDO}tkl&vT{B;2stvUEhqP3ZrbtYKt#wE5TYaElS7L'
    'ashIut(B9E@SnKY?>Ddc!ma?SXfNBACD>`RLkx}tgad&f%F9+Wu5=TdSA10QLfSz`QzLcCrIs`z3${76u@v#j{CdqiN-14Q%+m#j'
    ')Dwu+VhELafw9XI<S0tJwz^T@4^cPbtfx8MrNH6LPD3e27D~D+Ri}NbCW|AIy=^lqnO40zp`zK-+v3!ZhvY<L<_7->gn3bLY6&4F'
    'Y$PRYgmsn>WrCcj?M1m7ZwXQCJto>DPNXRqo7qvBtgT8IQ(r9ciT2balpzK>5Q-C<s5)0z*0~%I;^*u%*<rKV(1@0MhHtKWVda0N'
    '$yw%RUm(nfY)4uKU7`bK@Fi{9&c&P#b#JOrY%b^UaZ5d$R>$e-dHogv#00&yD)a4XK~hVp9`ulJ(ed508~^j^jn8i0`1qq6?XRBk'
    'jX!m0@AQ!Ni9!K=ZCWhtbg1dnr9f*Ux`bAjN^S0(gErsouo`{y#lXnlzQlhIM*HuNjoxvOj}MKG3=EGyq+1*6*pDwt8zWrEc69&S'
    'mqR1&k-J|!fSL7^jiKX}ZjG->)~}BwKo(~m7ahmk<LL8y*y#=Hn&b81;hQ6OM#t_ActFHMd0_s0hIOMQFTZiv`NKE7iT8bO*)h92'
    'JKHUd?uPj~I}wDdryKGT=A0DEJl{xY$BWKRx}}kbbauXCshyo&8e*W~-_|&R2QnkjCPiW#lz>*POMMq#89&SRpnk#J+~KPu>PA%k'
    '2qG_xch2<Vu%x9~@CvTJSRl59=J}*fXQ%Bk9UnW)6XdZeDep1TCfZ6Epg^Q!JkcA_;LzRgGyeCvt@OPvD}D1|+&$LMmtds<@KsET'
    ';I_LSbaqDG0c|+SUBB-l0x|X0rmy@~X`sBk(|FuF-q84!r^4G0QZ#mps+VoFi(Vc7jB6{ecL=R-vb?@u%gZgSKCisoQr9rq``*CF'
    'z+LHNXsSQB2gb%7cU{<8ltDDADEk-;thoqyxwLmR*OhMTV-I~`@wmo4lIC9wjkyOsqhqZmYA}qqoub2er=|{>Ue8CZ>6VRRVplu%'
    'FezPaSH<r+6m|>_4~&m%^~?D6yWE!?ce^it*rDMcbm6Prd}CkxyEm!vEq#?;$MfRJBP!M92*PomF6W`%*7b0+2$ifrZvnWh3*pq_'
    '-(kz(g6C`b?Y8@lMr`UI9KG$nH0~b0b6I=m-?VYh7*j+4jJZ7z#=xWc?RnQL2O>VcW*}allVbZ#{053?=(}!@b!or<!PxNl#T1QK'
    '=9^|~e0|A!O_*S4*xgUsG3>c!yQtqSC*$vb85n-xzT|59;Q$hS;|O?y#a`~RCXjvpGUWd1ADF^9M$;t!_rN_)`u7ja;TWQ6ipvf;'
    '8A}ylB@vrzDI^R$`jz`O29wI&PdZ@}*0|f#B%yD5y0k03phQ)z<hh<Yj-Q3|ab3J~Jf&JfFrkkA%q+SdI%$=cA^8%{Ypei^2Hf*}'
    '*?)To#!gt#M)vT~xaW^(@uw!@|LCGKNiP;S%aD{Z8a<dmjHK^e3ub&Izej<fd|Zf?YmoUQ-&>aN&%GjYz=@XYPwmM4%02Ms5n$T4'
    'C%04r^LQhJreneoia{}bS9e*WR7T?Oe8V=TFUBQ#k9I|R(#-9P${Y*kSQN8-M_i64u{xg@m&*(8A$K$n*K4a!>dUDsi$%NAQ^yK!'
    '4-I-+EWvs}|9oRPZay%Blor^FCjQDjboWcTAlcL1+w-uiO)oCTb0PQ5d)d!Qym>GE#d^)V%w?!I@1+DH3#@O`D`W2O7^Q$2JbYCz'
    'heYm-jgI`lauJCcJ(^KP5qV~{fu1X`kc}*y*{|xqX-~d#P%Rc~OMbdFFdc2^+1I{jomzPzyqVg*HoUtxaKC?iaCFSw?-_M6kqxs;'
    'BZuoAxy|jyKh%hglBx8e*(){>*ouoEIz}Xif61vXJ>5N*8`s*3t*wW0XnBjFTJX8`P!82^F;ol2w_9L?W;YJ?@IoYraf1)&2YUMN'
    '3{lLJW4uljcZNp?Y_b@-qm7JuoXr~uSK<O0)?=1GH`cCcG-daz6{K;3%hpJqQP03IhG-albW9t(a~Jr6M}CFbUT!26?<G9LfEeJ9'
    'tn+_rXu^tKZ=lq^v5BM+E$pk5lBo~RGHK|;>-fnv)wxr<q$YY->+P|%wMhyyjEz1RxqV6Y?b7-#J1ykiz+W$6rY`N?(8wiQ&n_Ah'
    '6f^-B+fPe1VHX7d+emadSLh!Hm<Jz~wy1|x-Lk1l1GIJgwo~h0xvKxjQ_H!Wt7}b=jg-q5#NQut10W0%9B|C6Rv@2}@BUDFaLbi{'
    '&jqxf&$)_MP-fB8Bq#YZC0sZ$4odYOw`zqp<ro7OWinN$P^)HvK~7zot%@O^G)s_Q_IuQ?dO`h~f9k87xMVfC)c*IiE;sX`Vf1cJ'
    'JueZ3*XU0Jh1YDKFSl5*O)hG^)^u{=w#i?Y`7(Q~4()2Dj%LlZQAdMT6Fb{DDx8kDPJ!{8A5d&9O4DyDHHZIy9gnZQzDZ*&Y$t_l'
    '?Orr?Z<fxr=0}ZP9J09P_}<vKjixqbjCGxL-+nMCSJ`Ntvg=!!sKZCqe}=H0)pj$V>rU#%FMj*c^;<VT`MCen&p!Fhr?;HKb-u4l'
    '*P`p1_32Hm^Zw}YkY{L+0+DCH^I-g+yWmeh`Ss0PH$S@m>;BJgUBC6)&)clKO|u_h`D7=fV*_Jf_YXib?~eR~9{KH7Pe%O7XV*Xb'
    'L;v;9Kfn3QkEK6u&4E`meQU>TD>cZU9FP~dUK6#fQbPl8-X3K%al}n^9<FFAuC$oW$&FSNwT2lo0M+Xb{~X#K)3u`ANl|;+FYqzp'
    '#G#S9t;1*YYPz%wtj-9k%U0e4*!Gaq{CIT4-6l%CZt;p9`^wu9+lmEk7iL?DeGM(Bcd9f0_S-8I`HKfb!?*h{#;KPP8#LqXIbjW0'
    '=f&&haq{D}bX{&&QoHCg{`EL~*$=#sU;g*?{?GsLIh;=$x76IAQ6%b+p4`xB=~tAr?+m%Y0X!UdFzm5a1slr@7yAsn|1O86JK?28'
    'GnW?LGVLk6x+Y=OwO00w^^EW?$=~rPx*VCk75{B*vs<zn+Su?b`v!|@sP<tR8xGvRKRo2V{pslVP<>p!Y{0cw?&`8v1^ypUO9KQH'
    '00008003MJT|&0SM6(e90JJy&02}}S0BLS!WpZV1V`VRQWnpV>Wpi_1WMy<^V{|TXdCfa*QyWK`-}x1@vwMYRBNR)%yAN99R5tNd'
    '-Bps?+Rart1>Dpi4PwsHNHsIE;XqY_H3n=9_+o-zh;25;_PQ4QvR+$}u>D`wYDs*`U%2P#o|o>K0Tf^M?x>VA)6@O*^ZwQ?S(XQW'
    'TP^BC<C?AKrM&LwImalQ(nQ&kF6i1hQ@3p?Z<KV?hGAPuv|Fv-+)BG~wfpGLNvYdfBdeD>-|UiS^Ga&qlX9t|nYg}G&I4EqMl-eI'
    '^ndEAty|}foSv#orv_x$*C<uWmLnC*g@R!gyjM#f7{DRjl7_u+se<l&M#mDWYHB52Rh5B(fxJH9Vyqgb?P%jh(Qu|!&CII>tzsuG'
    'l&y2BK2^~zLpO7}ot27)?Ib0&qFJ`CPMGkR(~3DYU$x*(k%mqKsW~S*Ai*D?7|$82T#^v96o8iUnxoq(q>8RJCM2`$ND&0+NBpyN'
    'r)rtfjLab}XQeO7rk<2!#NK_YF@O}~JaELvJwlzr@9fS8?iY14!QK?<l$3dY6n-uZ!JkCdm&o@odk-&?rJFzO-)c8jCH=fstRinx'
    'YQ@qkRvECd)ewGCyHW2gzMw!{c<t7L3+?TlZfm!DyKykzYB#RSWb4t{vu8V37C_m)`sypWv+;tg?U20{vhWCIk@c0%(l?zub7bKq'
    'yteoEf7oAwL9)Bq{nG=oKTn!#KkP4eZ(Z!|JteO<=ys?qfR1Bnpe|L@aMViKHW2HCZaUVq!f{5s0Gy_ysCfw)24vugtoTz(Fu~GO'
    'wys&Z$%His7$;I6e55d(>0{!f?8v+N{15vpzZ#qUg)*2#xB&zZR4Le!6G)<zDp=)eC6QKAwqqHUgp!oJv5Y^Kve?6*yT_oSghiWx'
    '4FeDyJs#zZDAW?9Ent&jt0mnk=-PNuPv~h_o8j04T27vCwqIW*>sLSMY&AP~F2WO8zt~%R9FkE;C}<xZlq!I&q*0>}5lT9sr<#Ge'
    'nIq<Ag1JAJeB{X6g@f(ZVaR~a9Nc)`+5H3fGWLl)n3q3>)pGC26HrX~*Z2&I#LEm&{@E}h171e5!C~q7L?x}@p!de}ij&4aX+>M-'
    '&<r^D9%#0kD+j-S<RRK_EaO`U)hb1`Dxg{wP_6cbsyq0lRn?h}P~AXhG%`lt(aBTok_^-JJXt}{*?dGpR4w*lWGFr6s%HqDqGmg='
    'O)xF;bwmc=WjXQ~PBYAeFy;c~-p}dN!$qw$p4X(Qtdy7<IRS%HBk3{DcwD3zB;CvdY{RCGQ1a*ujf+SJsIf`IY%NH<xA^CF>#{Ie'
    '2Sy*ayrfMfxRIoQLw__yw_!X@X_X3qNnq4--JlW*IBX&tv~;^#M2Y;^puv&`$QZKH>EV--trsAnaBS)0FFpmenDz!BK&7Q=@Tq*1'
    'IM}L_<?9DG?~}bd?eFh>*n2pK|0B)o<k{s9$-)jorfCS63Wg&HK&6T!u@J?gj%OKX&VKUQzkPb<Q}s6=fBEH^-+u8sRATVn6f%0~'
    'K?a>ZvK`nX-Gx2gf7ov9wi{n}o@}%mx4PHw4vq<)Q=iHet9f(;Wm|Q?VPz%o!8ExTFPDpv01Wgef>2TcPv@>vs9{yms3Lf~Qqm_T'
    'Fe^Ea$(74eAc&=1dSH!SRv@6K$HfAL(PB@6gAicGSbB8xfHuge&|P!cRU&MQycP>#5!ap?meQ#cqRIlMVpxs~W5NoN3{Tl-BK8(Q'
    '%Hl4`=mfw?!wD*CC+avvW3-#9m6S3{%mPMcHtd0-6OsO`Xwt%@n&R3)@8JWocCGX6cDwZwG`-W>?!5$m0tr(c)EiPUcivyG-IM;h'
    'ymPX%_ZP^yu1lRKYw&pLB>Db@1RuAhQ~oEidjk@eQ)&1_7i-0$N_hr+hUbJRKVEj8+`wW_rcMO<7S#!c{-kEBMF+z@s(W8>)BV7O'
    '<Oa(E>ZuYw-x8Sg#w-EnBb}si$fp$5=J<7|++{8qY;Ql|_K^tm^}?i)n*^NXT-nU0Wt{b+kV1#e7H4EWXSn|I;P$1?a=o*)jQ?#c'
    'fhZx*?QFh0*m&E1yWN?40?A<e&26S9pbMSlH=V6Ie1(}lB;3hUY0kWVl!^b3%shfbf5v&J@&A7`*&t$$2;mZAx=5z%lpU?;^B901'
    'qATJ)GV^aapv!UBh#oI~K9sVWQN&%u*`djTcwn;d9-8Y~fQkz1$%>jlXr32*1f0F<bm(}h#jJvFK4fW5E9EAwvRN*c3kK+9aXJYD'
    'dhVQriHUR?l8aK=bSC-i0(0(ruZ~SHLeUnCgti!dqG(IAaTOKH&=IDao=5i+vOOKLy})_Ww2yoB^FI&IAc7l+tEHI*Jpn}+RT}{V'
    'g-gSspg#}ec^@lp(ue^9OOLq*;Lp!hElZ_1#H<+e(GyB4Z;RM;8dS<L%xX}M06i)dBT|(J_5%h0MjkMDQ>0%?>5TM&FexoeN{g8X'
    '2l_PMD)HEOgp<Oho&)i~C*!9v_YvT9$TWtC@3(@XINwFL<Q!$Jton4ECgh$+!RiA}&5w=9wrs>5(n8@-Kj@YbsF?T|_F~A1z=u(@'
    '2s;QjwPxG7ox<^|QOv6)!?q2xpnAPcl~oG~zrUBIEkK&CL)z}Q0)LIGB@OH+S4A8D?6c3kLIX{TE!Xoy<5g$K-6O7l8ng%}v>cyV'
    '!xURJbv^GD){KEBEr5OG80Yz5;xiftx%@i-=P9kscW${>7DdFHByf5{Si<tpcJm6jNys)y{pLY^x!r7%S3B+3Tg(@eg=aH}$jG1='
    '(FVtAkbXdBB8N603ooIXa@TO-j4g1}n#NVgbTLVVZ36&`kh0k5(<qowlpBjHBJ7bseaC3w(Aj$2y}QF&N1-BO8Wv2*Gd3WI<~oS3'
    'wg%YnBWb{QYe`(F2Md9<!ZP;mE<)+qUb)W_Kh`M-F^iYege-wc&j1lMV9Ja=U4tbv`ZT;P;Ra&zGe|dvD#7dWxiKN6SJdtfvD`R?'
    ';^S|h3dJu8D-^Fsv@n@L6g*YJXQbo)MXbP^tLb**F>nu(-|ioq-J7pFTX&C8j{>R(1t++Vka84kCb7cEpzqHExrIjKL|)JZ0D{<s'
    'vAD%?0xsB8umv3iyTS<d0I^^IEMbiBMC&FWoV+3(2f+o=Rs&Wlqo$ntkFsGV=&%xaC2zvJ`kXt11Fm=(8FP>o-5T<}om8$ml`3_R'
    'CC#DMX;pKq=753RBsxiFQuiFGRW0hax35ay&=&@c=XdpW<`O#h_R0R*yZSX?Y-i)?@Cg|1Zq0Q!c3^<4JtmiK0!x5p@UQqC2H1CM'
    '5G(NS=@U>2!|-XG1Ru`A*jbmSbnCHr7wZl%(}^a)iZtw(f1;K_8Rr#e%mhGhI}hf2Z=V89A9UxxC+k<EB^y^UAy<N}#XOz1L1O)&'
    '*gGvl_Q#s5+El`W8*U~}u^mE{R8l&jQ0i?Oh|HXxm`b`)le(|NmnpHH)kP-aUnAp3d0Bt5V058T&f~gY7^>)dl_PvFt8;}VG1P^Z'
    'fUB?=a)#+(;i*uZ@CD!gF0K4Bol!jd^;%!2hoMfPs00i7dEzdfp~U)IK8QmEPlWVt$ZaI-{e^tHNE<{7s^ZG>sC;xFXRQaX>AFJJ'
    'FZ#h6AhCFT(lsy|;F&sOqvFOBE?Bi&3o|aC(7$yyUbJ6-&Cn%tYu&rM2!MtHnuXi~8pz(QPW|D*cdG|C|2Qf$KhS&gHCb$mj5(_J'
    '9T?!YZ-C6~$igeYK%yP7u<_YB*X%}wx_c~qaDekbgwd|P9a`^OCMauk^802Te}l{;umG4G()%dp$E)OFYUxU&PbCc0kL8dNwW_%q'
    '_<jl`DiKp8U%HtX=|U-OXO0kY@{lj-IN_&V0j8tkPxm8#ZoQ}h={OZg_ooB)bjAgniK3P12Q6;Bh*l;JtxTV)`BNwyS=s~@XgBVI'
    'W~EZ8Bb4TNHHkaYqf&PxE@V)#st^gkQEO8UcNFdhQOQI;9P2jx6{3(r!@Q`xPyc%YV;K{O3StXH3bc6)mI0FC5{QeT?<e3$O;o^$'
    'z={g4aUt}!3UR@p;4c$uk#HFJzpY04$t?J4&U4ya$#P`90-l+1pI!a|?t@6W-(2fAsQb(f_vye77cz%(I2k)0^1<&xJM$kb&&fcM'
    '$`z$6jG%U-IVzupKddi+0l*~gjm<yn@#c2F6Axoz|JjgO3dC?H#w~IE$&gF(l-5tYV^lZx+ffQEEQrtV|2E2u8Au^l3(J#AS=>9K'
    '-Ah94zTBU><GC*NYj}v)xOz}u$E{y)f_7o|CL~j_RE{a+<soUts>!qif>mKKS>fBH0XHUOa<|!TuC`k{?Z%aMYqh&q$94?Ynt@cS'
    '`}|FJd#|^*-QC+HZ&2?cv-kQqE8XjNJ4@>`e(RvtZM`7P2QY<P{*GLJ+k0}cz5n!GeSXvgOu4ORf__9z{3bK*^d8K0P&P~L#tv`|'
    'B*b{fj=w{|M84Dna6=LU$l(0T@BZ}*574cdQQ2$Tj>=hh<6EFGGAcvx7?qQwg1!VFYT=#m_#L7b)<g@vw~Of$CpyoT$?j$tp({IN'
    '4K!z&WwY%@9TbUi?qI79lry$6y?|Fn09kv$&jcQWsfHkTfor>ez>5N}H+m1hJ6OE@u0Hp<W}VBIFPNRBZ`!Tzy7%VDx0{`Ni@iOn'
    'd94KozT13)%<%w*?%YmjC8Icv9d+8wNnp3Vq^pKGQRbTH_#n-42Rnz|t1VAR4C2svdniXQ5>HDx!<m-69a7%?LC2a7d>Flto7WsI'
    '<t|dSeBQ#gyL+P;%5~NIgz$p+?B6&J4x55$7(zlTMYjP}D^IiAjj2?MhMs2^p{K<QmQio)HtYC3VlU(&_XfCo0B6`WNNfnXOanKb'
    'qv;N^%y8RitjR~E<T(e)^$ih9rd%wkvzS6>5haen`dlxK>z3PU8X1eD%hd4L5(~yS9<i^#DQY?0XO4GIRbUN+f>$Hl$DaWOoy~ip'
    'hMGtsbtL{kO`2hAYw|#tCyY~wC14d|duqZk^LPiwk`p8UK5Bzm(2KUWe>FCfN&aK)qr^v9(wLtmt8Zt?N^`ci__*`DMPA)e6c9Rx'
    '0MDMV+yy-P68tChI2)Fuak!GF%340JTSe^ZUof0Wi8r69a+fXLy}n!>NBS5gJ_WO6>nV_={DNw4QqWGZt3HuKJvoAkiCn+Cd5rXT'
    'oybM5WC{e}#x8V<i9gEEiq>HqUy+~GuK;>xS+WfK+(6`Zym<7;vjWd>x!2GkrOOo0D(QKqVLJ(dS;|QEAI9Prg2|LL3peraJcmG1'
    '?c$9~xF<n#y{VjDaio8yZtjd_m95B80MoAl;d1<O`y6sY>iZ--vd<Y%0J<~;p51p+w(rWlAi{hKRg`|5&H&3?bKx6IsFe&Br<pGB'
    'Y}ov`1~!|Lq&GE>xqpCH|5cU64pmLamX6-b{UHGUbOHPY?=8~FlP7BQ=Kw5?nq?WqEP48@_oOvTE^m`dTeIZV^MiW5y?<?%{e%F$'
    '2t(?qp9}>2bo1KiZwLI^)t?(Z55j|478d+TBiX+n6Ou$3Fzl?Yx8K}8`28cYbcyV}B+cE<avdlJ6J~eMWZ|scQ)T~`IY08O-8~l4'
    '>~nZfgeUG!8Dg@jLt(E=3;|ZV2Pf>lVueC^=%`aL<lv_rqVYwSg^V+@kk`ogrGZ08`mqznVh-8D3r4ah$LP6BWG~ij@#P~A^p)I;'
    '`T8Kw&cS|DcGA4wo4Y|4{zTRqt}5JJ1^14LG|%E%Xc^O;rFC-chOa@m#^VG%#uk~4MN|mN(tXIjyREN#drhpsLkihgb+N<etKL0u'
    'w;hbXt=M{cP`^d4-2X7t|B28UDnD+$cP+v5_=u8$pN528y4kt08X}MU*NRUb<2hNsinZy=B1Og}m@mw@J-V%%FpH7vF>r}Im>-OC'
    'hpb*9mv19CIjnqM4YJbq*z)G5EJFSpP)h>@6aWAK2mk<F3|&-Tp?PQw000jn001KZ003!jW@U0^ZewLHcW-iQUuAf3WpZg{ZewL%'
    'WMy<^V{|TXd6gM!Q`^S&yMKkQduJ>)j=^p}(b7&Pp}m>g(oDmhCX+HA#a@GMZP_EqNg2Y732(|PErf)S5LzfSDTWZ57!2XR+<GPX'
    'Q~$y}yDQm}ER5@6jI?{+uRUiS3<d-E?Pld<)hN&7x$)}Hqj+P~SQv*zJoYvasFde8bVp3*MNP`ek=y`RDedU<3wZvau`+5L?dnT='
    '`fSlcnmB>iKMVH*L7*FuGP$gxaSGx@m5VFLUPy`>(xePqPHKTbpzFrvFaC3-D}L=#Pfyp^-PgHJ?pDa$8m=5Z4~4l9E<UrLju!E&'
    'Y4T+5E*Xp4p<cjFq7;`?P)^AvkgCS-W|i;a==&T}BqS#gPv6s$s&#PWTi`B!LLWK;9DG1hr_wb`{B^uyo=)iH4f0)D*9%+r`V3x}'
    '#D{Op(`QC`2QSRv{pHHZtXbT{Crh9^QJoN$Qk<OCxH@;bN<IqG@`_yF3DBMQxTwG}mcUz|`?30X124Sfm>Jrb4ceFi+L-nnxPbAc'
    '6jD-JLkda~>(!VcE|pc_TXHhAH$g^8m1I>DiJ?%KQ#D24L^(-C7v%vyM-Uke`snqPKxy?lHMYfQP}ttE^^FM}Go|sQRJ~uWloo?{'
    'b^FGR8~P7pm2&y!&6`17oWWz;un%_DCm-t1hVd8yuN)t{btf?ZPS+GMp~dsEq{VYtRU$FVBUw`hI;d)OkuqQ;2+4?wL?zMBD<R;_'
    'M=pFSFy8c$|Fq*4I21hoQSZHf3J`Ual;AX9rcTnKDW8e-DcO9Ej|vgsE9H10%-L(P+FC?m4<U<7ND#a>W75}&ghE1~QDKr%s=uOi'
    '%ThKa$w@Jt25rAh4t_X<4-epfG#ne!S10YK<;{DIn~kbl;!*0i+Ixv97$=lg5l9)fwk}5LUK+_fT@biWI=L8w;zQ=d>q>b#L?G(V'
    'GUH`aa_TFo@eES>ka#<dn7UD_uO(iLDNhpob~c-?8zCPJbGaz7LX!}Q*0syW06umOe9RRHK>i<{1c_vi3S|x@`5a_4EZT1|!8cew'
    '6G2fYsXQf>cah4wNab51Wi9q7dBki;ZAZ%kOITVNsI4X0CvB~KW>{{1Y%L<@a!5|{p4cFtL{`=$Iq%ZT$9-Xo5{VO!N-FpaxqQZ='
    '+=1YR2cMx>BfO7rGP)awng>S_STsgNQ<<@OqOTg|z%H=zF+2(^4Ap1@0Z<&!K|(+$a(323m_;|Dl!-`ZZ+${g=^Ba(f{%%50$HUh'
    'f17tfP#-r<WvF1l_#U3zyLVCen>p{og_lN%drT=Pj16L?LW4CU4*}SqWw9Y2*t(*wt#jhZHv+AL<pTk_lp}q2UVpmIPHpq_1W!MQ'
    '^Iczlqd%W8cHU7?IeV8FH>pMJo+JWGyim&yDO^twB<F!Cz*!-9Zjt4w$po+&Zy8NscCFi8_|*4QahJ=eCozao;{##qft+NLtnqd!'
    'hCetbTr1vGX)MSc4PF7O5+3BAn?tmLL$r|tO8b@cbGd-=BLi?Ehv`!WeBSRNtD;6C4Y5H!T2>R&aY}-ChNqmki?;Oy?tLyA<1Vm`'
    'Ol>36U?~@&k2|^c76l-Bs8HW*36j#COg70o$%R3L27VNe)C?a+$-DwNr2w^5aFEd$s+3+pR0SK&Z*xAM5@ktpJJ^D?{V|(KV<ouM'
    '%W!BY4E<SH&{u!gi+`}D8}hQcbPA0x9(y%tu>hC{L6*}wg@juwc(L|5t7*q`Yk2F2K@#7!epeWRLI4dG>#p&_9No4LJbRb*qV}$p'
    '<7AIC^@2l;U=0#_P!IcHy>`F|LL%RLeA=pT5Oae7Gi0Gyr!@n&rWyr;6Dd$tR46@h6(wl*VZB1~i5(DD2{zJz!D<r?-WxZH1(1kM'
    'XL@O0U;7D4Vs){I3&(i(P=7c9)(54H<0HKB3Kos>TBTIbAH6d+jsP;GFFz02qYM!xmC@p44n#ct7F=xtU4l`9>(0u^w=)R>z2F&h'
    '2gMGv;mnPkNFz~Z9Sf+(-w~)`?+~+EEK}r7rF{zF%~~c3YWK{&zT$ku1tb5>N-}S;5C|!o9X*g+kl5X6v)T+iiJd@X3S>*+&2k4|'
    'CwQze&3x_V*r*W*G;UZfvX8^&^X&x|%`67RyS4EdG;jo3qz^&hvxRQ}qN_GvUrML1%HWm^y&(t+q<OOXOUs^4l1=7x7jw70om*C*'
    'FelyWt!YVaDiYaD4yM+)m`sAGDyOe)>QnDvW;TZ(;j!QGd<n1a;im;AodcL7PH?RzByFI+>N>nqN;bn$qj>pQ7aU-#xMwTuS|r44'
    '<e-H~OJF5ylk}wE!Uo%0V3!$GFbtwmTL**n;PR;CBM0x{)`)8<?iJS`5q+OPIgPtY_3l!Xtl}tiqN)a*rLEq*2H)dw;c>MvRXI9x'
    '7OHRMdaOgS=bWBvtB!0}m$%#4bxQ{~Sg%N4TZP=Vg0Xt2tp)HapvpB->u(kQnl%m*c&FshRCtAFVFyAx-9eCO*>QS})gEx|g&MsE'
    'YWh?GuWsYDIb6E$ph?{1rAfW%Lz9x6I5HO3pbOPk*P7e&wxnqNSD@MRs!GThuV&4I-R1;jC=PefwX~S<C-J0+r)Kf&5_q;H2Pg6h'
    'oQ+zm4z|G0au^XoUwUlb-*3(pCG!c<CIE+LGuXTg3M;^GpatNJ_}{GJCl#yM&|g&_FEp2pl*gN-8}lP$4$l}{d*<$N<?z?$WHd=j'
    'qsvIqq?D96!{LmYu`^{HZT)pTsjn8QFCYDNJk^|Xztq={RIP_F_yXNQX<si)7tE29Rs>bQlhSElW%0&Cb9V>+jICKbKdtW_;KSF='
    'iNW!Oq(1ozQSQr&eZETLu}wU-LfX{jgXY9YTX79$i2*xd!1?;(slH#XKK<F;pJ|Sr%cdbl6Jq+hCTe+qZ=OD=l%^mkDy0>p{M-3)'
    'aVgO+fyFbB0=}vp9^j*2fs(#7sgI1|@juQH5h?V%m;IC3oc+aoaZWI>g-druCF!psjes_@xB*YCkgA6)q~{r7F^VV0*_AOgKg~!c'
    'B{d;IiI>sYIcaxMllbj^t-&HqmFv2m-P1#=uwM}v5@4|FeL8g5zaP+_6PS1az?w3cJjgsm<`Hi%1(Qkf4}EnCd{+Bmg4}qJ#g};R'
    'o&J16f4yBf8h6fDK+oKLYdks7=V0*^MALUR2+GnP8IOwVMsc}PE|Ub`e%;3C>JNADtNVaoDb1R{4&(7zbK-@$ZjDRcv~LvCYrg9G'
    'A(VSYyA{v6yG?)B7T@5b9er{EKX`PGYg1xIN)LRAGPjYtR6-vac#kv6Bo%O}RGpX)T<`h1tNZhwf5op{{j#h3`qi)44^zQP=_l|R'
    'Xi$AIL;5i0Krj;2L-MXKmhsaSrXhWw8HOlknMK}Ng5b@EU<$f_TENq<^haw1zc=tr*QI}VcU`~Uz?M1&ya2u-XioQ#cMuB-0-ian'
    'j?J=nQXK$Ze>6wgjQj>s$|8)4O|1sRaB+pwqm7ukHEPa`P%**HyQAj88kDVIufsh0(~;`LEO0SSih-Jt@ylOze|Gir+K*3zL0Qb8'
    'U<cJ7LR()vG*6!p!grz8_G_@y9*MROg+0(ldDJLRz$t*ITY#?qGFN^4n^9cX=eEv=*7w({g%vzIZ|;qp4{aIrc&YmHcq`DG=H9kZ'
    '9yccr{Gh>tpMCvRcTc<pi{beP`phQd4+##&a8dlg$7<`v%IQ<PH2tJMTRjh>aykP3fCjw!cnmB8g~){wYs9dndI3m2QpGGS>CHt('
    '%*E9N9IJ6H8;5~O*5XMC4j`43Xa2UB_>SDWQ3%y>i9sIfw-^g^#>?GkJ8+D)L+!?sZ&(0w{~Q02gN6}yKXh!G!z)mf7>Ip!VT~Sx'
    '_0hw}wjG>XAU1G$+}|E)7wVVSeg^j^<B3oY86PkBlktSEhm4PF&Qi)1rFAkxmgAuFNaWMdxx`bFET$Rq*$zvkvtr$2`plH&7VrWq'
    '6YNl7cV*Fb%Ts!x3=f`P$~`>1_ZlE7n<iN_F(}BLzY}TJWdXf|1yn-PzO757`pC7Ce~LP8`lU>q<O5&>#x0f`^(!Wp4ECm#8ue`|'
    '1-S)vs6hQ<lVu(WxGu4|3!N6?`y6jEXyd?^mNpJQkvp4gUbb$T!(7aLwJ3hi1NktQk;v_54J%BD0b3#Pe^5&U1QY-O00;m8Tnt@*'
    'k|9`I3IG7H9RL6k0001WbZKlabY*ySUv+e8Y%Xwlr5S5;(^mGoe}z`t8Oz0S6K3~A-Ay~pEFaoo;KR0G)^?4s<sh(Sk0kSGni;!8'
    'p5#HNc@QT}cm$GCl0ZpIh)wffT1&P+g`cqJ+^ffxC7Xq2EUt9Vz2|+-J?Fyne8^m$G}oURTen(wCXAPljPaKzjhcBlZXT@sS*d>U'
    'p9|;P+Yil+JMht}9~eiE&3lu^<b*N1911n-^PF@|%$G%7E-JCoO|E&kYcAX|7M5GJTdkvgiqM#RZ7w}KY0R1{_nL<_W9ELRQg8qM'
    'ee?Gp+rKXVS@|x+10x~1P%5fAcSS2I_PZ+CZ~5ZLh@^6&hHvRYF&he{;f))%zhf{8kx(d<m2zCJoX@9q>4u)p<RwuN@HNh9x*FjI'
    'KE*K};@}U+Qt>;LT5EBKN)6&0^YzZ;oUt`$?4lBy$9wp`c4X|;I*Y6A{e75(#i+5ZTJ!a4>!&00UIT?dta6T1iaIB2vZCptl934c'
    'C?QYj;-4z%WmVx&aQyr`M@`_+{fNG`_vZ*4QGl?j6w}I=1eF&O$?`v>!NGxK`QuzJmx?5ZMo&k__$a6H$srhUL&S^QnE46NebheQ'
    'fPtd#f!mxy1A@iCI46xYPzgwkG*0y(;8zg=<m4Oaf>;9NqmJObQCS5?;Nu*RL(~PB>rdcE05C9s=HWd25P$;%Xy4yyA3rmy3-Cj*'
    '^X)-9wG2Ok76t}@K3cnl02~;=Nu!Fh?3D4iZtQ|%&wx@8))}8Os=qal@0-<qoaz!|=I74(FLaM351pdh{D{rzim|?9tUq&cyHmc^'
    'lx#N}*19pO`J&2QjdIsGS+T7UlXa<}35*X~hQ}eMN~OFAzKWY1v-+K!JH<6$J#XEsLK<j&_bSkTk{Y@R&)H5A1J9RB5S&4a6Wv~R'
    '>I2;DI2aQvIAH~NS}CdpF)x23rL%HDQZ&dvX-!ge$&R-k;ch1{n#+uZN1#sNz!+~Bd#}uuZ4l9DEI=9qmoTO#jD?rx?S?u3h~^1k'
    '^nIh8kv%9oIZCod{u9s$A7M!msd`|BB?wLo45cVr03lkfo-{W3E{mTUCP2Q2Zi7@?5^v^<Vpg!mC@1Fg#p_bmU82RgysYU7U_V9F'
    'o|QAYqj%%kgr)I`aZq%tzS%siv|m3p=N>c<kGN3^jC01`thsbRl|9C_>OYtpw-}|Xl2*>63P;Cmu^9t6%pqyU2p9{%CuA=1dK!F0'
    'l_d=|WUcqdg@qdS*vbqL*RvY`GDSs~m9pdk*yM)2#65g7kSI}Ngp=}`#3dL&%7fK0Q5Fy<yfBm&H{C?Ot=)cR?O1{R{bLF2DG+p#'
    'hz|*f;ouRKtovbU&1KWifLDx|SSm?MR<JQdx^2-974?b)FrNZ(cs_QeC@X@yBhno-bVH~|96N3l7|QHkpa_wn>k`ZZ;cqR<klQ3J'
    'BbFoq7DR0K3w9&QTS3$_mx-64`4K@*R<huis-Wh=!YD&%ERxhdgrr%Er@~R&g)Q44B{_Q|O7T(qmkW|A>XKlQU<5**hUwDSh*~U{'
    'gdtZ`pbJ%Zz-Ss5246Nr(h@)r4uzpqjBF7i#JTAl_b)ClDGq7s)i`6b?V!ArFJ`U+QVWnExZ*%4)h919<|F&Pp5Y5eHx!wOL6wzI'
    'R9PWsQYkNk{==Z)wNFsN1aVht_`hQZ5&Jy3Z^ZPBR}0WPP5^V@=HTG4mU@m?+p}o}okw?5{@M}1p1kA)z;x~&zT_yN2QEy)Ibb|r'
    'YHb}s@!NX(xOw;soXd1&XXOCOb9x-Zzn$75b0&Cl;d`E4R1d_~jDPeETvyBJXrND)dNwv9=`{KQhsCI6C2P6YN|dy1-5ie*KL$ZQ'
    'YONmQJ*MI@2&g6LPP_4;6-8Pr7Yd?!(=CKBCEQ3u=*2-AWOZn8&|e&pYR~-sZu@bS9>G=}G_eBpA1(O6GGGL(m|p#6PCYS~ciWRM'
    '%=L<~wcS}d23s+fLH%7qg^v`AS=3@e0LEuYxr`#r@{uSf&^m=o;a)ODYeLTyl!nf#DzmEUOr`o4whkwrI+QH2QVXR@2-QHr9Pg_>'
    '6LCw8PFjLA$Ny1z%%f#7vyobk`_xu9O?gal1t~$3(xRNkHWyER%RD*ouuCH3Fj~C$3#h$mV|<6U;@p|fx%Tm*v9<xm;jl$Vd))b%'
    'w9aXeSCm!G7+*JzmiiW~K)uHyXLA#BPbaHpmqayl8EgE}*oR5dZXyzZ{QJU}XU?5-3Rbd*i4k;5Dcg144ASMOqoAdLnlj){S#OR}'
    '!$}2_bT}MgW>h-UwbO1m!e3v!co7x#EsP(KrBMg1WmKpCX+3>9WbQr!e`u{8Ko>yuPyI)|=~Ek`jbV<_1Ph}j;Ejw_(z&x_faKwB'
    'EQy-N5IaqYHD4b?zVn!qHrRg3Iki~uEOc&8@QZjE!zIpH+Hk;LR)eLncY4J6oKQNeszuf12QOt}*F{y4l@S5^UU1y@!*PZ~dg}uo'
    '+kW|?eLP8Sm?obXGdom7K3HXV^J6hBsonG><N>lK;-Xlnp^UBP?Sn^gZWN1HcXworGv+sq?b|fTwWcS{y<cIKrHDlQ^x$9$Gj1r5'
    '0*m{kG1V1#CynVKRDr=oGlD8vSsTlJQB(W0udSnf%5!@gy3<wX!V2_g*0+q+C!Ng~kiYD!qh0gaVtaoMukNN_nR_!ddHZt$$%T-t'
    '-|O0HzTR2gk_yEuau=PTc0ZhWvpL^*vw62N@Md#$A!Ws%+v@w)f6d%;iT@M_zBx1S-@$>8lUfp7C<(H_Q-${$JZtc5!h_#a@Js`2'
    '2A<o=KPv0+{OF};OK@ij4zu?0MytN-T)zb}BgCw0Hb9|<lSAE@{MRLTQ8@RYeSbC(Us5}T3~}M0ixPihPQAoBJHXwv64W7(#;q4}'
    'r4V6Hb8rs-S-}}U%o#_E=(+QCNXP2cZ1K8+jFO96(1!V9iL8V<|A_)x?Q^2Atn_Z6+%+LzmS%t+b@a*P&=4h+&%CL;@1}R!04x0`'
    'MdWSg(naeKqJ1J3)X>nSuR(Q~rCFCTmep^O28d}*>=tZb$kVOJz{F)y^>%k%*z8gh*9{MI;n8qBsCh^05yHXkxy^ta3V>v;+SMvj'
    '@pd^<o+UmOOuRo~Kwm1Bgh(KqWu3NBgwwyFu7WXk*KxGJlu6^-?V#t2ibU<^nhS63x(w*?1mMU81=TVxG~_?Td({mLQ`Y=j$_WqF'
    '{vOJQp?C}PVXub!anggE{u~8z^{$1>yB073(s(PF0FmqkUPtaLSAarjq6QCB#f+X-z?0J8pKt$PD3Ytqo%_bdZJMoVbBtBe_CsUh'
    '1<l-S<<VK$>pZyYJ<G}YB5TBhd)*Yf9)U%X_t@!`0wMnisLRM_@0vEd7x(HX!nB`om;RNW;w`zEPb%bp7$VG!>YLH68INv=L9dCb'
    'EGjx)6pZpXgDcP5<Aw8Qaa79rJo*0W+&SL4thSjVccTodp7m9id;JekO9KQH00008003MJT^N=qmm2{905Jjp02lxO0AyiwVJ~KN'
    'Zew3$WpH#~ZeeX@E^2dcZiP}!PJ=)c-uDzu*Pg(u7*AqMK%mk|tJIL10Br-7K#Un`LIWAV%gCD<R=t8#AQi_%XZgOr_cJ%6v3L`%'
    '&bd}KTHHLxU@cg*pInZwTCBwZZNPW~ygQaA7!E}Cpfa0Wd^4GSVAYaGI+#y&{s0Bx_(K>EIW-}GTz=3gu_TCNzZ3E_0OZTUQmu1`'
    'VV_x331arPCJ{vn^%4n!{0=<At+25b0i8y`0W~w~ExHJ41i&*eV&*PIP|qheRl;-bFTT&Q-0Epd1%WKAE3Y%@ny0IKn6E_TQ&E|l'
    '!u#f*eTp{LFP#G;uYWLmpUj73+vf&TNJ`j0vO+(ren<qZbcV+jA3+r9T4OF^MM}}GpSKlVk8ZyJP)h>@6aWAK2mk<F3|)M$o=0r}'
    '008X(0018V003lRbYU-dZ*ps2b97;JX>V>{WNC6`V{~tFE^2dcZmZ@}00O1w{cE4Ep82e^;pNo*Pd80}vbXo?gx=@NHz-*tM1xhp'
    'Q~~9cbPys>JJ(|hKI>|DHfQ~dE%RS&S^Q+zE@Y|ayIZiBfFbyD;m+r?*FE3e|9nClrZ`v&x)j9iXeL7i!D6vowE$2{0|XQR000O8'
    '09*`R000000ssI2000005C8xGb#!TLFJE72ZfSI1UoLQY0{~D<0|XQR000O809*`R000000ssI2000005C8xGWMOn+FJE72ZfSI1'
    'UoLQY0{~D<0|XQR000O809*`R000000ssI2000006#xJLY+-I^V`yP%ZZBV7X>MtBUtcb8c>@4YO9KQH00008003MJUELB9;Rpc$'
    '01^TK00{s90Bmn(XJv9OaCvP}%WA|h47}$nOdyb4@&Sb&n{8p)h4xsAQESCD_z^6n(7&&pI7weR7-l@0k*zTXC!`!_o+C;rjGIDa'
    '0p14&Ho;R>C>yjkUT=^-sjB4pdOF=bid%6B9;-^>n00M;Q;AgChF7qns20kM8x)3?u2q^VCtyrwC92oR=Y?x)O4y<tO*&jqXeA?Y'
    '-R;fEIE`F9`gickVx+@7S7E;;JaI&D_bcC~H}%9S%-sFg*Awy(q66k6-YgELZd#x>HV!)y&rNIb1FeS)UqsV7Bm$j4-TkyLGlNpw'
    'ASJN-^)-2bb2i<>A1=f7k7|lx0u7Amp#xaLU2yDcl`a<%)9(Ggr7uuR0|XQR000O809*`RMr6Kd836zQU;+RD6951Jb#!TLFJo_R'
    'W@%?%b#!TLE^v8;kwH(yAP|M`{S^~VkTf3Lc-dr6Mva&C2N*L!I$g=OfRw6@@xMETt{Ppn7Xt6`y*IRtz6Zp1Ok*TOuzm0`fp*R('
    'ove35RpI%~_VW4)Ht<>je^NX?lM^+F-Z#{t(>)2bhJ3uj)79C%sJ}UNusFsp+m46<_cR?B2Qrb;@4E3^*^x_VxXr=yRvGVFyF;VR'
    '19A0bh^lI+1+>o2{@EebIuki$j1IQUDyAJBEcm3mkt8>nU!(>f%G&6pF-?IsLy8L~TR-zom?9<1huf-#c~=mMcS5LqF~zA6zLfb0'
    'kQuAMwo3s29<UDNTsJd-x`B$ADfD6}B)G^6NBv+IYo2uYchVAH7|Ue0e@u194^a5;P)h>@6aWAK2mk<F3|;VtQUfUg008O&000*N'
    '004D#X>2cYWpj0GbYEj(Y;0m-V{0yOd5u!dPQ)M(zV|66oM^Jy2iT+sqaM5%WAtPUL6{n{6cDJJjq%-umbSVzo8>fo!}l{l>um>&'
    'wSDY8G6t%1)<@vR*vO-@CR7Cy)*Y*gU0*#sJwM*wKm#wYRVA?nj{zeS++_{P(ze$z(0W%vS~FuSG;&Ytk@S9s;xWNyaNJ5nmkT=U'
    '6oH9N_sYl(IDXWYry^^0$dqjEt-%fSUN?lS!f@x%NcuIvd6BF`b}+~-=q#67bN#Ddd{g&t+=AER(eDykaIN3C*fZyCt2CC$={o%o'
    'd`dlyN(znqH}A+e@lhtC|5SKbIW6+DLVDBE-lpfFW%fHsYEUhxpiGF|2wX&@60ru<0U_V^Nm`Hj=Ug84qsNf{9OEV&&|iEj6EB>z'
    '`T<Z&0|XQR000O809*`RY()th!vFvPa{&MV9RL6TY+-I^V`yP%ZZB|hZ*6dNb1z?CX>MtBUtcb8d2Nuv3c@f9hVOlfu+tRr0S1EL'
    '3kZ5IbxU1wZAX(LzP;JzCPQ;7&6h8KYg6=~wauIpY7Nf!F)^4BA{%xwkWzJLh~U&3J`3IR6@A|s#!7)yQ52VaBV@>g4cuP9Ekd7S'
    'lj}hM#7W@0L#n`vK=Wt5-0W>k-mo0RGTIun62}F{T9<^K-NB4+rhP*Vjs1KNhXqk1aU27C<Tf^obiKLm=&v}T{6hd~mrGZD9aGvV'
    ')^_`Nnsle%=~cS<i3_gYP)h>@6aWAK2mk<F3|(7H8O+EB003na000;O003!jW@U0^ZewLHad%~IUt?@(Wo~pXaCw~=TZ<gU`h9*y'
    'Q8L)Az3nENAfxjjd)5oYByRRamZ7QXu9<1Kr+d;>?e4k^h!REdf)G$t#DIcI%u$YbJdzxLjG5i|<X<@7SC{VUnMnv|UZyVJwZ40g'
    '%3?~QXjI9n<WWS@>8vOvVR>FiCetDposKyhGQrP3Wp@)MCRsYHw^J6|^=qQY?OKS;E@e99oe}a&nB=&#-0VJ3FmDt&w>VXq0h5rI'
    'Wl=7jmHcYOg{;NPit(72gb7?l(<13~B3O~W-G}h+`JHI%(#7XrdI8RlI|P0-u7oV6yo}-^;j~964fKWJami)0&yU<wVDD~$Fjogx'
    '4|x7$boR+}Lwgk$AX(0Hxk4G$EGh{{=QlUa+3UPaM@JDn7#u&(GQlkeb{NTGpXUNDE&{=zGw(zfp1ZOeZSU+{+5t@{?Q{}8A_Bx6'
    '!($$>BjA~j(y{m0WBUiJ91FjvsEvw}fI<Ozon;jmASe1dOeLQR&p_cX9U&k>*cW4YV$lQv|4_m-2>3Igdw@9rxfHY(Z49DiOvp&t'
    '(lUbgtxDj~XrrMcR>?^;;Vj{0Efu;+qy^~LJ`?7}foI(&tYqwmLY5;u@o1L?q??fLkaS-q-4}b^mwVkS<b{`asjnzbxe#oO<VN&x'
    '14;JCal>X#L!P7UJX*=o#?uB%q~n}nya1jKig0-TDFazQ2S!Ag^8?SmiR({;Wx|1#C9shdiU~V=?z|r+=@|Un)4XBJH6jynni`4l'
    'wTd!*Lq#k|)kraV6y+e5;APIHyibp7aoT=AnA3o;Yz$=NWZI{QwxLCUi$`68+A4kXVsKkO2#~MWbE=Vi*U-l`9gE#_%o;SeEsX%E'
    'wRhmQid;g3ZR{S+RD`kFEK6er^#tajXGlC@CB#KvKeU7TTpaV6T!&<!=EfHxBr%#KKa|!LAvo%HbOLwKM1xTnPD8LdstB}LmC1&b'
    'f;BrZ+QWi|@r1|w(WDR(O$wMQTeLuoOqiSu<*n_V-RR<_i`%X_47kw~Rwaw2H)BUx!4jkDjFnOVk-b5kK;lV}#wvLj&=8{Aqxx7-'
    'p8(9>pp{B6U8Ew-g=BfmJq;R=B#outP9Z2$npeE>IOGV2EX~1LmCC@`qb6X$|0twj$><tF3I~MO{FIn&Qho~s1a*b9kVa0G0N33c'
    '^|dQvEJFrb5~hC15S)$YNrSvsBM$r(lp*-TetpWp%)JK*+-OPBt`94ULfg|QMhGCRU{)NO((tt+%{_Iia&0;@{%LB9wob_fc(5`K'
    '1KR|oRuh&``({x79Kmc_cO437`JMHe>U5!$Ys!)prme1#y9Lo{DP-J#hIt|axuIwa?%`_$m^dRq;%wSPeshM7uHr2-gn<FR%K_w2'
    'PbuDWm5e7<zVF16eY6bx0AEd!5|E1|o`_{l6j)#VwG>$jGjsm{Iqn;3rphwIW$#>X@E}eK6y(<^(1z^&0}{-DJ|rhu9<7H2F98Ya'
    'e1s`EM6VDYa`M}<@Iy@FWLy0~27gHApn_qw20_`$jZ^?@xuS+x>u5GWG)Ox_jCX@_j!tOxmPf86V(p;VU`D3v^=8mV&%i&iIRvya'
    '<)VL9_s<YqeQYa9nI?UT<@rcJa~ElpzV0BGRVLb@`?~Q2Pn^zpscSkS80VpsUM`EIit(X0u)l?(In21z5~0fxIf0`Jt13^h083)J'
    'Qm{G81@`M9&{8zVSutc;s|8e@<Z^@S^lMHCfh}6<scZncc&$@^H8ogL*2>4UEHc$XXiPU_^&UWR-s&XRki*uiO-xgmFS~@(8`YD;'
    'mL43Mo~=Dn44ihx4k9(IK4^g$Apy!Rom=eza5pT9%ri;Pxko!j=olHcD0*xF=wr%r20jqUNt*Ac`PhyxbK6B3J2^hJet<AffrY4s'
    'q&P-6`=#zn*E(lH^4E}au8~@r%cduH@3cW4A*WwoUwr(@y`SDbz5d<Fmp`0*_TR<rk54{$cX8|UlTUxWd*=^0CtDX@B7a@`@bu2D'
    'zi)qW_qWd%@83B2?4#2gx9;BldU5Sra^~zNIsNgy#hbSQXYteff4%#mhJuqn-+ar&y9(yWMD4O{c1j$m*RwEvoEcRsV9$&rm3)*Q'
    's!`?`B7aUOqQOeTVwtn`hDm%;i6sZZam+nr>tqt2JWkH|Zs}KY0`2zf9qyQHv~~tmlky-~DK34L`;uxp6E?#I1Pp*5Ak<K!laRkM'
    'BDE;lDP(C<Yy(X>Obw6(FJHQ{>$Lt&Dqkr<Dt#?!3EKjK+FMDQ2B1m1O9l;WgWYe>5G_JJezLG#$7(Gy!+`|=p;+QIQ=pk@h?=Tl'
    'CM!9FXUwU;Mp$gfu&%Ccbljj{MMh6j_^Sdsg-e61KrE&5(>tFmuKnxYAJ-ROe6@Jz+TzxK?*8`s>G!|W&QeKtKmLR-?(SaR#uYvn'
    '0JH^3I5g84P=}lU?}^$%jwtdY$4CyT%=Ho|gYVAqBY|SL*E_Shxe23l=YHMDT4~$7u(kk5BMyGH3L(AbkW4+=6|X?yC-?sO?~|Y3'
    'I=y+5bj1^0K~8SoSbX#Iy<fiV!2xKe!#`MSo;u(mtOK$x*aKIlR;Oc8pIyOg>!iNLh>Y`@cV@Z2{vS|F0|XQR000O8^I8mD@qe1}'
    'TM7UGFChQ`8UO$QYIARHUvgz|VQF$NYIARHUvOb^b7gWaaCxm+ZFAeW5&o`UfhyCnl*&3MxqhfFPA9!fZhX^AI=0(<a6Ae`f)Zj9'
    'U;xmvC%ylEy9<y6U!2&Pc=93v*u`RBo_zpkMi<Q8mQrn*EJ|%0I~O<Z<n3gV;ncF^#)>sJhTql;;vsI?I{MxbN_0^O<+@_^U@I6#'
    'I-;s7Ho|lz!&ocm2Rd?k^HC(d9m7~F=Lb4+)|toyov~KV4+Oc&$=ogzt*qn9*;@4M{Pg6_uj^l4{rlwilk-=nXAAb*+1od#!c|6{'
    '-<F~y{?Td$=T{C~UTgRb#(wrgoweR-^cGmt`sOVS{~Sg;65if*CUddg8(x;J)EP~^hM9MMVltT|Ik%Rq{SO}3tR@V89<j4BmyWre'
    '@P1*1a66sCiIvmL3K-;0Fx<tmjYxO}Yurv+rj=lOa9=L31bcRLbOfK2u%tE>S)4~REAnj44qw0$>n^%3yY!aekD<X%%6ZXAPisi3'
    'R&Q73T^c)LeVsnv9;z~y>TC2deW=D*s;|+<{Gl3SsaE59EtPcZ^{kP?1&ihzWiN=X82rQjKTN9L(kxvCaDl~dAUoRmTyM7^yJ8!y'
    '^TBM8UMIUsU9EGWwr;n={A0)Ls9gqD2ws3ie3Ofy>X^OexfPv7GI3+G<S*Q~_0l7D4rw2N0J5FD)$zvit4@GT>NSzxm$?&0aVM`u'
    'e!HOR!U_Vv+r-GyNv+skx*g*r?I)#T(lttC*0{A9tBr>0gFl?%VXxR8^lG#p9pu{0YEXb0<%)R#k2Q4;2<I4_Z59kzjD~0)o%ckg'
    ';s$Nf6@U<&ojYy75-?{k^PF7?QG$6KtkoIY?_{!L$qt|;fwg!5KsU$|eMOM$n33C^V@mJ4(U7=JlNytuO8}fw^dtHpG=@S@U%}Xh'
    'Fc)0$t)Mlw6p7?HOSlDi1Jep=Y1yYrJAohwrr$C+%QerdUT)OLejJ1PEOyI(YpjG_p$2G6II_H!4zglXIRMf87DF8>gu;po#AI@J'
    'z)_F`7;|U@S(PIaFWEZ*rvO)zX}DQ%L!wjg#Wm)EO8p0MEfj7l-GK!sgQJB=MY@c(L~XjtQ^r&H%eECaX-8uQJ`o(lh?}BCKH4Ny'
    '@T+ieT3abd%oNLZ!K`LGVFYYU^NKPSPZe!`D$1NEqCH9|6{U6A4MFX;<Jy67`P8U0OnRJw4(Pj43V?9|6|le(zq|xp7nh?VFhd{L'
    '@VQwa0ATtB`bHl{hW1qCyx63iJ?3U>7wqxlt37^N5j3H&(x3Q>y*&N(=;-fpKz71=pYQ_mX(IsTOD+v&ybY<9R7I6@f)_|;kC^Bm'
    '65^OOi7<{sAPbG1Fb=s`ZkK+c-ry3@OLQa83)sn)z=cG-{v`7uM1}yQ0rCp0IyaSwOMX{di7Kibn3TuwC3}qtbi<1>?;tXxT4YaF'
    'j2(QQLh(&kYzoEq{`~<ve$gG9{&6srd^(g=Ls1wUm3}rN{p9Jh#RnL9x*`yGI;yR|XjJ=;_t5?UB=57qhj<6duTF!+WR1T^#=J!g'
    'aI)|an9Ggf=9X;%2MP@^j4(kTkc;L3>pM{yC={6xj)ia{^H{Po$Rf-%Pz1+o7e>1kaE(BW*--bBhKz#*v?z*X(qk{~isrzG2`Ew;'
    'k--=0Jz-bcpQsIVG;xy%YNc^jp6S2=;0&oJr~qM*%OILcOS{{~YTdDVIq)ayISvYYTx^Usjc_vH46Xt<_DJty6ni2nOobQ#$Fn#b'
    'iMk5_T-y`ba|TU$*6HZ;ap0SEM)7BQUXy<lybJCU+#qvFb|RssEGftY+>{(-pU4HGh@CDvO6Q$#;UpLMe-~JR2jt>mr|37|CBTY^'
    'n{Kn~7o8WAEcTaUHk}T)3G>|>*izcH1-PqOI_kYRd~yjCX+EyPr=<xkjVg{?$G~k+bkx{zOo=^RsDONgRn3SH1rfNaOyMSW=N@Ru'
    'vef0sDVu3zbt;{G94;BQvDS7%c`796^VTgO0j|A`6tz*QD5!rsW>0zz5?Tj9!VdEaVrV#N&vd1LYNlEpxTwm9ycJtJqzRsA&W=I*'
    '9Vs%J>~+FJ3h(hBrF}lvsk{hV6Ia|d;}j`fd3H@E^>Tig_fhUsmK=4HU*vTKhdawd5Ai#+Qwhid$W|Wa!0B>=p5`Rpp#-%pAj0vu'
    'B!-IsYK%7bItK~~_CIVquTW_AZM!e0L;M<LX(9aRw?$0ZzCgPok^ZfjivNCa-Rkt<gnXvE4_;epY}pF8#_wz1hoDEE&lQ3z8<?@b'
    '<b(^FKrWv7JBYu3mPTJoXjJ`Fr~RM+yI0rVsi)K4?$13wy+FEUAQ}WT2cHjyDd^7w5cqj|M+)9i#?kf>&<0%Wd*eCPOwXK)FPU@k'
    'UCi-sNCRuU&%5Suf9V^VgzTl$b3{?PTVgCTA00J(7{ogxPC~=rdAZ}W`FwQ`d&5)Bz+nH()HV3x=<pwxxDQbL%0Ap~FfEl^3Bjt3'
    'CC495J71w@?_+p)2b<p!QE<`4^wD&|raw$C@1w7Ns+$j62Y`*a4c!A^fMs0ffZI@DAE7-lxm7KVg+>K<@bymD&#VSP;u0t1z<aTB'
    '%_9V1h<_XEKQ6u7dFG2G7b&MV*-B*9=AZ0e^<%2J{lQaN^il~YgPuXR{S1f#-rL*9BxJDw+*6rNF%_KeO@uG__>wueBqC%rR)F$R'
    'nCx(@i}>{Zd~L4^rqK%zVgIN;JbUXjdnL;?0H}j&Bd{LZK3H~rG>veJ9yzi~C3JiJRLSHDpGN2{3EyTEixd(EsatepeWndur_<35'
    '2Cj&ae8d%FTs4#p88Mqb9H8#K<yX93a%&@ZCr0P_z~Kuf)0=Lh!sqYc{U!TF3yU<ZVS>@YvG9CW_*V&?WkAvWTSB|+yMzd`6^{5V'
    'fda8_ico*Y`^2eNDj?sh6sOnCV`vRB_4oSWnXZd?Nqk6DU#8a_?-S(vS^pGs28Awj1Wt%8;>zPn37hazq>mXi8o<8RkoIzXIt=cN'
    ')b&D4PtpiYrM43DYQgAHSl~t6kzusLv!T$ih&C?Avx6+M6dgBf44czaCU9D>vz_pF?743epPS)7S_)bRI&w-giM)My8*QdHJh`%P'
    'liLY2=m_zifD>6A{@PF^6#@3a`vWGSm8B922})`sGJ0F7j6vu|t0q0pDd8Th`gJhKNqIRLQbp?6@86G5r`X=n#Fux}18@hR7h=-u'
    '!?2h1kH@^#{ZsR*%<KF?G{99Muh@(ppCfDL*tri3{tiQlkrUyga|k>!m-rxmWl!<MM|U$6$qr-Cne=bvn7oZfW-7!vuy0TIZhCT7'
    'ht2%tE{DdyjEH<0ae*eaf2u8uYXI`Uj}mIcaQdwsUI;x#Ag3dE80oBg5Yzh_CY~HczaRqwS&tX|CPszBAhrOHwg{)vX1P>d5UH0B'
    '>!g(r<kREW09eTy*y3_B@<3m|S9E;)dH8;X`ll=M$M7;c4F<r1@Tt3|g@gIzPf$w(1QY-O00;o{S`1xIl)K_93;+PuF#rG>0001L'
    'b8l{6a%FH~X>u=Wb8l{6a%FH~X>u-bdF@(Ta~ro6e&?@1=?9V?ij6aUqP=RB*v=^9$f>O~)6r-sa2FKU+M5?xNo<<`-tQa$dm&e{'
    'X)8@H_Jg(D1<pA*mk%E<!{Kmnx{kF-^QcOcxQUZQ6tdJxn6(n$zkPioRxv)T@+8S`;_Q;&e{=FuL~0dhv5E6+J{So6d-hJ}*`b)v'
    '1Mw`(Gb7_nm51(IQN}46Un|!jtI{RDihqh{+LVyXkIOtyl*|sf&r|X9T9s<<e$-uHtrXWXi6c=enF#w{sN14cTB}IJnaDFG@)g>-'
    'k9fX>@(klBQ)#r9>2<SA`-9qA>ET+IGBm2xLS<o+JK686+^AZDH<?5@t>mF3Mn4#5Woj`pWu-<_F<Qw)(?ed0QC20%NQ|qDMMe|n'
    'tHycOSNZat3Jp6*mZjWS_qb<nGCCWHGarD*W76Y(O~knvosVGYk<jXWg@Le*99ayrQX20OaR~#&awEb#O{E=Ot(aB(!YWc_gW#38'
    '->l}>T{R>7U5~6l-<yZ!eLtGl)^h(s#wE<ibgYew2jVDGH5D?8@Z8uw7#y#J&QoR9)Dj`MDRVrGrAT9~=^ciqGDJjUpzyg39;E>i'
    'My5rgCL%UA?<-`z6f1QjbWC5wT2&<(5o%#_)WL026xR6p;6NNj5x?$J7~5pvf-1#QC9%53YqOSyKX3@iEbD2Z*Ljs73`*E^gZRIa'
    '!F3v;sf0eSi~fV1E+fg7ca_Fy*2q3x$Pl{7Wu;2<YsyLF=Mpt@r3Zr}3|Lo1k(U&B8hR+nWLQs0PS#Z@ZvjClmmzRs0H>feT3qsg'
    '{BMe!f7Tm4Se1Dy%%<Ro*w;tdW-4CUl9-B9b!(1aqXe#1ndCAW*e){?v~a$19m&@}@*CHtGI643lpRK9Mz`C&QKl-h(@lZ282qIc'
    '4xEAmD5(~tV9c4ws^HW)c~%5sIGpnPl4UGxIP~1xeD;#6_8blQ!M4>`als-Nqz8=&3c5{_{PL30q~{ZNTt#!|4HWkY!AHP<Y?E+Z'
    'Wmk(tWtV0>M^FsDe&uu<gfYbk<T6ox;wfQ5)A!}^V^$qiX`vU0B@CzV3?|~>P_Rp8v)RLY@D%t*4y3>TyZ-CuA&fE=faI+EdWuTa'
    '<}8AOGw7L;vh(wE@A%Jhch5+^xVh_Q7Egdif4^f^AbFgQ91tE_yq(rlgDE#O1w<;$BSmikt5bq^Jgy`lX{Ra87O|PxDgB7-=cjfo'
    '#hA^?3nEc6nZU{2>4oIjOqhx1Y9*`0@Ju8#q0+)^T(em%xtk4QOm6Kcz;ww4=vp$&+d)Mll7=}@72=cXYUIE)dbV=vK1I|NQ%jO~'
    '?zpVt#2jFXq5d=OKc0wHm4(!qlmU0*)ux#xIX0NvEh}3pgb7n8;shVu<E1T|U6Ox<Q>0BNgww3q04p6XVoNLufjDL+h`9YMBo9FM'
    '89_`o6PdRYGzG*KKi*d)cG^<HaygGc0_r$ZBG0<{vx>y{wHw2kEza}Ks9q9nkCYC}xG>-iM0q&3;;g6)tjuAMp_%^=;wMc17c{{x'
    'i0oew&A6b*T%fTwN=Ds5QSCS8s7xbY7E*_C+~eK1%G8eF<h{;`Tjv?V%;C%O>>!bNY!hy3Xa%;Y8i)<tSTXep!T1!hmb%ZRYb1qq'
    'CXO$&oFa!(z1Ygy3(6^ORS%^NWnv5{>y9M;2Xmw4hA5;X`n)>ge%dCYM0OY~1;Q=uE^4fm9r!sBc=E$Ht!iweY)=F@`RYPK?`12c'
    'V{Mse7*=UTVL%fM&B`Q4+nfjDi4Xz9x&&**6byHEOb$SIT0DS(gS0uX+x)#!s%R%R7H4d|HM3(q5c*ySsmc|&Y>iD=RuyFqAt{h2'
    'LaD|fRxePv0SwpF*e+`F67Osg(f2>vB@bSia5h1TiNDc{7i9^DJ5ST_t<4UyQfsW8Sz0*$QwCFaCY{sXspu6=MbEdUqH~M*bu73%'
    'iQ?Z0K-|Jo`UWj1ydK>;DeWgY$EkxBogVv1XzJl)>SatjWiaig*VM&p!h(_4+w`LL4l9lL6Es^BX8dPtJ1YP}(;U-g^JBMvJz0#M'
    'xf4?Ek&r~S=Mz7;Ndr>1p(^ZJWkz0j&XXR;{XVrlW<L_gW~8ZVS5;z5zMe_}L>02rt>#+!R9BxP7ETfo3EEfDwvv)~>B>`5fD<G='
    'Ra!>?+LW@~c=vD3_<32Axq4KOsFJqw!%jIBWrbUFs@sBWcBwb(_HMkdC*meAuX-jenxjZO7hByuZd6m+65ho+=G9xM#w}-0t>Q^H'
    '8qT+!c4y~<yFWX}9`Ph&Kl2_!mTWLKHWuD=y8z7JMtYl1x$MN(PBIYbo^Pvm73O}ipO~_ChqgmzCywXablqk64?*2Ok$itP!A5Hu'
    'MhAg-kFn->ltGm3SISO{o&{d`GG*%L2{ke4O<vXP#crvTd8o9ud3tQ@2Hs66ivlSv%e>0$uFOSyB?SeI4TP;1-SP>jG%h~O+54F~'
    '4Wfn3U)b`evgQ9SYVaFz1N*m^AlTXE!#u$??9zoUGjK87*=}g3d{9$`JJy*&v!~OO?!ngY>2{6<o_F*U+Mvrnwvr>Tf9&fvs@ich'
    'I~NausQ2%5{1#ZcH;VoqAnI6HK#LnnwvR_uTEyD$e0ah71x`N~P9J)8d*6ce{}#7@f5_%aoc2aN=&bDijc|S!Qr9~k4zBS;Cw&jb'
    'f&a4*yANM4JZkHl02*jT>lv-<@*{sxR=c%HABy@`EpE!cJ4I{{l_~!@(|EV{3v2xXvtMAg7iPI<UZ`yB%ZmpMLfJuENCJ2?gtK8b'
    '9T46NNbd#HkA(C)aMxk))|LeZ;qW+2Vq`2`6mVy5jB(`h?#Um+BvQMJcHh$7kt{mkiw5|rZ1xe6Ytn+8PMae&5|Mv(FFkBo(mL#T'
    '<xM)D%sDwI?ZUQdbf{?K%--{$8w`9gqbiGBD}CQt2K_te1=Qv$R11nwXn7jbiQ%-l8UhsK0_F<~+C*`-E&m{%ih~0?I$OR_^2ToX'
    'HF)5@cWTS;XHlcPGT_Bz4wU3=hU~QDq{X#5J$m!qi&H-!x;0a%FkZ#pa@WkCJl&KuDn@G8v-U~Lo;&m&?4Afg=Z`e0wZAaWAPucu'
    'v&l^SY_Fi9u{|=%VP1I?YqhH<&LY|#JwAE<;^dT}7J5$V8(;1Q%!&buTH4c%u?xTWdU6*$QD%!5w^%&!d9Oq1Z(kh~B9tt~ca^ZL'
    ')$;|fz35UL9ap=h7|(e$)8%1Um0Cm<R{}@bt-<&hcVH#(Syv<S@bHl9@@(Xe!T6#KUBB`kpyxl0&L`b2(rtH}TZ7_dps{(|e>UK+'
    '^~{c3^|*rqzaqmRg5euC4Z0du?Ft_H{qY~<mD-{&PGmhvJ*B<Zx^!;bkx_5ixGi`YMGG&cf!d*0F>LTHBfJQPe{Q{6tE32qca$*_'
    'r)3M-pj$>vhMuv0*qDR3rE};jy!Fn~hhV5NyQ&3U3EVOAvteK#d-JZnR#fX<s*JprC0xfKuw(3l;>bRRcOYgz2Y(z6?PZLu!j>ij'
    ';t~+^tKUQxbAqq8&DjU8_A<&&`+FuIp&4kW_(X0<xWITVs;pEY-5}#L>H1cQzMCr6gX2Fu=XcNFm<Npc+^<ax+;O@+1Ke%jYU)Hk'
    '634crQo?q334>N<J~gMb7E?@Yk(Q({BLz30e?aT&I8xEfpTCjZnJZC$z{T&Hr7RQ{rMyh#oNj=yQ<_6k4Bw20_8js<b`7We80d|j'
    'p=4lTgZT7LVZs_eaii?9jepw$-N&lG+f^xzjHl%#BHYjy65Jri9tL(uf?I!gEN?fM_un?L*9on>&H?fxzEDx`F0jlN^fZBd+VG3J'
    'S(~@0j_KAJC0%TQEzM>|r?o#a5BP2S_<Z*ioS}kN=7DfdgdJqpc+#)auC1xd{c5(-ftE<EZ0N{tRz9sJb&qCLl%~P(X1KF1YOjwN'
    'Psi3=WCvZEV{8kYHS|2~!g2e@eSWf8+5&B_Wj_t>GJ@SRujw0&>7#0Q<;&z3xO}W8zus2_Zq4<ivjY2yw7jiCqJdb3AySJ4d3dql'
    'Gv~!3m2tLM4DYt4t}UX87|_1}P)h>@6aWAK2mte13|-K&uuqo*006%T000^Q003%pZ*E_5WpH6>axZXUa&u*0VRCX|c`k5yja5x='
    '+eQ$*^H)sS1Equ^;HwfDFkGQ^>wG{q8U%tMtf-;5@ruiGN49myfA7pLC6Q9>mIs1d&gXk?hDBpHK)YI3YtWn8HU#(IzDyR^e@w3b'
    'y7}~b6cu8TZAlxK<+gI9Dsp?<yZb2{Eq<Ha-h8~dpG@yk`19`brzz65sqS|*_T0ZaTP-*S;WO<cT!+`cmD!@}S=;4z%!%zcwQ8{0'
    'JAC%X*X?N(>7rlpC<4AhAfJ%!+|gai`{GZcC|Y3w{_{dLjoJ+ynqma;(Iy_kyGt<I(X8LsJf!&*K6uYd7>&k|%o3Qv2g!N~Zw5&U'
    'N&7a0IVAH0-oq6*{AY_*j$jL44LthZ`J78pgezF?fV<u(=W}oZ7{hGtU!=mUg)nnSgC2g_)pUCGZP=D}N4AEi5-Zr*Euf~7VJLL9'
    '0)B$rSSPFtj1?a^F2Oe5v{a7>nlMyjAK6Vj*i1JFjvDs!Ir36TZN8{AGFSY*P&5b?JeAsDuu+`UKU9{PuV5C>;}qf_af{=jHTR(F'
    '1v?VDTIJ~4P;z;gMko5>>~VZBVJ-M9oZ(EF$3!hUc7voh+_fgiRmEM`HDg7kBDlT%=%BC-Hw9$5WyR8Ao1jVG4()-ZQdbWgBypfx'
    'aHIgX_R=^@3s1;j5Y|>KIeK1V{vfq;BzK4)`HitpK@SQ>!VbfC67PyjX{q%}_Dr=gZ1*gb-t*Rs-o@F;kq5G#QI)e42J)1G9=>3o'
    'H(YIxIlX!wYj_J6eHTyT-YpDYC~Ytl49>KqhcIT6M=_w`RyuB>%Z@_CVDw+fWFFZkIzo6uf_814(@TRv09{*Xyxg3iUka`qRj*QT'
    'o=RQi4JxjTS-MDJTN!jtKKM!7t6)Ffw&{wszsoZ1d`~%<-2yi??FPR7q1W5_kev)u+8w6DKyeZH3~#$cqv6yoGK_4NW%185yt@e4'
    'XYo%08@nF#PUVWf`k5E>1gL%YDQkHvXoxZyjD&M0??JCC8^lg=+KcrXryMc!qq=J0>2fK$4IAqyMNkC`n3IQ|ms^4*@^Bw_;QSEb'
    '^^Nw@R{MoPng<a5oG_C<Gjqf<7-Eivq2d2y==Dn4^*(Vm=2{uU+kx5zy^@~_qt5#a)>yS0Tx9H5)kd#N;yVje?2U3Hg-6}p8G1Qc'
    '%V%W2K(y#y!ejp*Gf!XOx^3nZhx1=ugYvTrEQE2CgwelHO9KQH000080P|W5UC2{I;wA$C0AvaP02}}S0BUn@ZeMa`aA9e3FK}UU'
    'b7fy+Z*6U5Zgehid97A!PunmQ{hnWO<$ma6G!grliAi16jR6{{bVz8R$}~>n)wQG8<)MlHzH29GlR_T?kWiEO_@3)~j(r_xY7XWp'
    '<#Gyqo~q2i==x$X?jH~Or$2xF@VwX}{Dt8{`x7O#q0;CQdvbaHYskzZlcT4U)v$lHl4G1;*rO@o{rvTciUrf2$KyI#=mGxaLEQ9|'
    'W}0PHvJ3ac^Sp?~;D(W7K4;RjG!t<MWQ8Ja=zd*GF{dDexL=2ZiHcZP@R@ybp>*iE;VzIy`Hh6o1Ht7I1SX*d>;k#O)xZKD1W=Id'
    '!NUA9LJ+(^L}xjp6SydMLQQ7d!<Ynlgwj^ohzUOD21`jWfS<2!l&lir8uW~(DT_eeBaF%j|AEa@^90&t&K?>vS4RYCRg&ffgGhl?'
    '7zg#uVwN$-@W4&t@}Lv5pu$N3OfWaauPHG}Xa+}MHT0*<jP3iFnpWG5V$eC4)NpGl9Km=j(}@2=ZX`JXa+8r;heS)&Oy>87!`?OK'
    'm4`0;SpMd2*v#NlEQyR-<d(Ew4}bpcWY8atuX<;{2k&Ni-obJ@c>hYmf};UvRVU)M&Wu&rY1Syy$YnXgeLz+W<sAG>mLDGyE?ADY'
    '=5Y_XBQ|8TidT*K>Bvp3jk)!58L>w@d|02i)i=x8LQPvS`K}h2T6G78@UM7!J^((I$~t{np?>M^dUcvX(*bZ(RVoxzDkM>n@ut)c'
    'SGhId7>k8<6PvXNf+Za3Dv{SX*OUnYvZaWhE%znvc(_x@p#{|mE4JkRrTqZicWJ*%|NRqvvkz)Q)eqh^r8-NzvqoL3&K9jY`)Sn-'
    'I?Y##Kgt#?d}CEgTcKhW@G?xTbY#frB*F6^kCLv6yG3Wn)3DvxzST<f1-p^$LO7@P-6!o=sqQjHXCLkmyH5*V%v@WwTDB9m!#$ID'
    'VV>GzcDaWMy(Um0sJ%=@R46ne9x702ZLYi0j5%envIbO)H-my^EtXM0?-h>#b#`{tW`Wt^7Q`OTWy~e^eh^BfJI__DRIR<Z8^9{}'
    '`~r|e^4piarxl*6FSCGRi}(*vO9KQH000080P|W5T|OK{PcH%h04W6k02=@R0BUn@ZeMa`aA9e3FK}UUb7fy{b!}p0axQRrjZ@oh'
    '+b|4$_g8T4i;X4jvipLiKvo#LVOf!81B#+BRGW^qDUu;6P5k%MC5fHX!{#TMJmfitlu9X9pqrZVCD2tZq=v=KRhnIXN-w{Do_!30'
    '(jt<zrc5Qd;7Sv&)gJrh`ul8-dM){)sqqN=O9`Ht0*za}hhO^NpM<SZ1;Omcw-4#ua82jeV^FAXm6mrfvBxNUHhTWz<*V0k-o6_q'
    'n4S-^Fb;x%md66e0hrGdJIb0!g|aK~%0*0VfhD+f#@DNRl#xPKjv;J8VGP5I6IY($4Bq>q3!n-JJP<|;kVqK#IN-LHs1z1JIS7s*'
    'N`u=a&3Q>5kV7ftifH9}`dnsA8_!|rVtGZRn*b}5CF;!nXGBM_%TkpQDM_<O?B<)~YblXym`sjmoJ{BQ>5VJhRFt7_Pk7-EMrd~j'
    'E)j8|TOJ`j@`({R2xC7Br=)BXS=h!;d_A2w*dgsyQ(oZKrbdVOKWvxXo>pc1dpjK6StH@lUXqTIGgwr3SPm}~Y^bj6N(A4n&$5tQ'
    'o4i@eVOi&Xnt3w-DHyX)<DTTd?M{Opi<7RHu~E}!cDOf-%$SKD`?Qk|!#JUeu(~2q+%=*rbLTe1-aA9$-l}<sNQwa{A!YLyMut3U'
    'wZ#XNba&f>{d@3TchlCgIoh*Xyd7}c>3G}wZd&$|*#^l;17{Zdg6aQIl}wnC^^qY{*jqyR;{x2~SZi=EC6!Wi$aYn2MeqkuO9KQH'
    '000080P|W5T?TGKjk5;;07@GG02=@R0BUn@ZeMa`aA9e3FK}UUb7fy|Vrpe$bS`jtty$f38aEJs=daja9u8W}G<m!RcU+gYX_GW#'
    'yX|B=9!7w$FFsC05^<P}|M%{$bcb{tm^k)>ZSGe4rQd!s*J=TFz0AvN$QMhcEnK{RHyWS*F*^P0)!W}YojFkqRyHrpV5&-EMQKe%'
    'dwy|-MB`sauV1}+bul_S@57(xf4@DGcBRXU^-@O1KNwYxk%yI^ZN=f#ef(DxD``5Nd>+fsI)Fcp2_IhyZKSSFldyOEq|@ol<Q(|*'
    'SWQ02sqGqB%ri(fL`e^hjvE>}3D4lAJ9G?LHiYy`3YYM5Ar}*=kKy}n+K06M+=DAf?^E~*PQb{ID_Kq@s5zwWy-ArR^2=bBPwl0#'
    'y6KlI7{Zr(rbCZvJI>3Q{EQDt=K+84dYxOfgu5GA!dk6B=Jp27pCbj}e=seSp}Rx7Wr+`rG@vw--iS{Uax1m4at7)X9-|XTmTQSp'
    'HJ)P`cQ<(<fxR`jmUcY75qd0aw?~|kdvw}&L;CAH<+QrE$q@tw&k757$o2CQod;I2Y9VFqMDpuPC-xZ9jQmMAdfW+!&u{Z3awrN^'
    '7S*!SEX5RYQBkmt28qVO+iovnp%&8S3-rV5*KZ8W6snhN@)im<eL#evzU)8&NtS?7AcZd0eSDr41)%xmHF=J#N%{#0Bwe8YK$W!|'
    '<kI4=;gC3Ptglrr)O9y{N<Ybe?0AAKU7;w!L6G%RD^@UriJU9EF9Ej_Rm`tTMIKE05xd^DNC&7NhVVS}&VR@Wxs~b;sf3=AQ&1Q~'
    'W9>mfl7b_rRlpip8AMWEGLAg0k4JRXLX-miM(+wSy`_NoxKg%xdp`aLM%r!^{DZ%L`Ezu7G5%}xKE6=Gv1PomNhp6Rj3h6W67}&{'
    'Ia@7@e2Q3))YqygCa6tYe6@j;gArkkVTL>_xYm@&8hIp1{lLY}HWXqEP`t=m7jFkCZ^&^I39IFdqZDIX)&`O%Ng!G-7fZX2Sne|{'
    'q}vVDONVSWiEPTY0QwAG8RvP#rqEife{P|9OP7FVOm8IFHfOz;T7Jsa$~b(?;1gXj(A|r?bRa^oL%^DqVQgVWVf*>0i$1A6vh1DO'
    'Qh{WR-OUuav0Y?5z>k<0dY4DfuSg}5B@5*CR+Vy#0TR?aSPP6ts&i9Gye_MESAFK++jf+9R>O2Wk{WM$U})bPMzp283#?}|TbVZ_'
    'x{U^TjU6u!23HIDQMcE61Pu>ttr_gK)7kolZ-kVd7s6WUasr<=JCCs=oj-;Ud3NWKSi=p22UU#a4KT~da@O5T9y#&yM+QzI%Z*<+'
    'Zk{(G5+3ntIjq$$_Db#Te7mdI?eW!)Q12)E9d!u29-|S(dtR<$OuI<M%-Ri5>JlwRY%_W7Nl%Ap<2g_RlTbU;t@-gaw)hKE&?Oe}'
    'K6N}-y|$z{qGU{L4cAfB{Q8<&(voW;*6)RyAwU&%YInJ-RJT$tLCdAcxoQgI>v~%*YEjWK&UWX9)&`?)G-{1~UlFV7i&)i%xF)0z'
    'TI5w<BDGe!(Q@p^FCCV^g?&VayN}UfkZd!bXen;PHZ1NTPR`Cw-ZSs8*QjQWs@}QkleHGgU9PU(octPg8sFs6ZS%JM_DE?>-+fyf'
    '8Z}q_dg`E@?}@o{A^%ciLhrcKAp*`ThxV((W@B^INJq~=%x0|QQkEoVy29QaVS&N#y3kwSE#4`8yDf%?NOWFRB&*MtS(F6<`-UGN'
    '`I6x8m@cM*EUsM+o=-c`V9$<RvAvr40nYV_VG!&a-iD3gElSK^qsIxt_OsP;Azgol4gMlG3<R2^#2jZ$H>W%>CVkg3CLJ7pt?4Ft'
    ';}uD>)GOlS{7LfMA#wU>4W1@a=_aI`7=w%03cJojB|plWTGEu(`^o|C-;>w>jC!4T`0)b(>O7%5v8D(43@EIAid}@Uf_R_ftmz9T'
    'nnm+Zen8ZOQ>!k~3k1|Y`l7(TA5p(ph?<5q${Kr_c)DwcFLwxi-ywYSu<slb<XUtF_VAo|svaql-=oB|*#tmtcP3YALt_Q2?mK9`'
    '=n4YYq0OGCtp#cCnvS0Luobf8nXQfB!47&CPKwpc`y?S8$Jch~K3Z}Zy3gQ@q6Mm61IcJBxYF=JO^E9(96k4j8I49X72!RxPMw1R'
    'nc}ZBN!w7_z7IN+yxBs?<y^JDraHB<t;b*w@7kS{6@sTQ3ZImzg%2xZs|_exMd+a+y@pp+{lo3(F}%n+hZKy5QP!rN-%c2=a)S1N'
    '*i>LDHCwIy)8Oq}bO@2R=}g-h(_+)5Rjb$3BUSqcT1o^97@BCMm^C@q3#QFYY}RX>INR0@9t@|!Qb@bq<AT<i2=5~oZ<OJUuUX+n'
    'n>)Hrux-9UY!uRPN;f!XW1p9PNvOzWkT9?09zy^hyr`eNNbXb5;}`U<DzR1M1#rqsP|jEa2X#ZsHy@56B%?58XJ$>?(b+*b?5Ih*'
    '`=D?%4>;~B(O)m*;k?`s8=Bq>uJ*u6e)FY^3#^M_w;i6zB46a(j_?J0WJY(~cbmg%5smGAZgW+Nq6v~9$~F+)kkhw5ab@#g14Xqh'
    'Slq-%`}|!^W8kM%qT9_js6D^~*ZmJrO9KQH000080P|W5UG~-I_!JZX03v1p02=@R0BUn@ZeMa`aA9e3FK}UUb7fz1baH8KXD)Dg'
    '?LFIa+eVh}{E9a8U~)}!SDD$_UDha7jz^x+#AByyWl||y#Wabg2nhr*xJc$Es@f0P{ki>;J?C^c&<!*$q-am3YE>15A{u=;_iKBd'
    '##@%}k|4Tf!8VE0oL&C--TC6>pUz)?d-3LvqtO~i%o3hvVv*%33|wqPn20n}UoPV~6g*nQ=^`q^P*3B0*gxA%7DtOP+%A@3yt=g~'
    'v!AKCiaZFj*(#2*oJV=4-*|ud4z|De!};rrzg%3Nzk5Gre}4b=%{!47X>_?ug!MYj40Gh-Q?6%tDgXQ{4-1ivM#0*K%zVV)M@~Yo'
    '(;id(HZJxZeKQ)3e6eQcna7z3*K_7ogS-iQ{&j7Ka)lH2J!~<^cWhlmt2~G!mdA`IiHLm0*=oa6zRF=?_%`1N##gIiTZBB1Q^sLP'
    'k@!3p^eBjY@rfqI4K3sx3z%D^JY3*RV|aVcV3Sj}D$-O$IekrEv0vDmI1;qcyhuXvfyPx&uH=UC2c}`q&hXjToM%!FKmg2WKRE8R'
    '{2oWKGx_*h$hQ_eA6IXt1Rq>sN~1>#O23z&{h9?CfJOen;x)VDVc=I_%9w~&VVnscz`_1j#5o{W&JfJRjQz|az7>d@coTW-K|OmP'
    'Z^b6QXB-IpUIZ!3$TN}Q96W;GauviM+Ynq49ZLbb&EoJ*E|za#YZ{3FI2y3xsaO{w+lW-?qr3|FHNL;)5x<5ZaT)=B<$|dI)WUrD'
    'ILI<QBoKa@;|Dqo&7mGG<oN)guXvcrM}Ax^L*cs~g^DA65e8eN3V512zI*CDm0Rt`7H|v=fTMr#PP{4e{@F8VL?M!1mdvq=w_6|;'
    '_4)~Wp^&GLxdKSXQMi)_X7@q90ZhvwbQ%@|J;G{nA)z(ZV-$(gwD+4J6mnz89`qrKb9}EMGiU@p#8I;#<6%^6$CLRZ$XG*0&sxoO'
    '!Y+|OkbEO1wn@HI2d_iS^E9EHM_ECjxfiU+B$KDX^(JQ}x-3kboiXpJS1mad;qVf@178_4{qKMMch6ijq}slF#Q)UhcEMS}W)&Bq'
    'k6gpq@0Dixy2O!Y`WgF1tT=*Wd^t%AqzX2EH9;z;k8dWZrGV`u=@@H#Q{({St=KLF5K_Fn5v!ctid{ydiS$6AfVLEM%;>F$@x4gL'
    '6G`$plKdKfMcx%{ljuRInVAxd?8%v}YG!Z0`Sbb9%f+|nKUM(1pF4aEFmq~FQYjN_e6UupzzUvc&t;Gkjj<^@jIYPmaPJ4=8eAF4'
    'O@n%hgtv|h(nN~M;Otp0xR0RXk}th!Ia#&ChKRZZTv_a4kLYGPc%{i~M5RA@mdIJaaZ4E521N~oC+uQvp3e~RG<>Q7800`Kz<TL@'
    'kfF4WKo}Plu#hR6<f69%RE=jPA#9^a&8`MM8bPbrhmWBA0N6_)blBF8`GMper4x-aPW3@>y5><DC`Aqh6^7@@Kv(9$R-hiPbS2V*'
    '(mhJ;(&m(oBi#rvh#9K4RH{p%gmoClNjH`o!e?%yG02}9Vij0=Dp$PcU=Q$YXJW<%?AXb(XYD*&F}k_SQ%Q_-&t_4SM|&;s*B7t9'
    'KW|-v#Lg5Y&VzS#=t?i%y?gQFfF-WXB_=lQw&_J%i|KDj^tXVl1G5I%Jyle<K`LDb*=EQ1GA1eyGJ^?h0;EF%nSl*=&24M2o7;wp'
    'L~A)?HT?yy3Kq+C6q6PYa)YgGX=zeFF0{%W@|>gmAuOJ!X`GI|SFS^^;}F#p=urStM1o5`n$?I?G(?gcePFN#@@OnV|H-yJXy@7%'
    '%5H;Xk;jYD93PjT&Kl+cK*s-cc3N!;XJ4#bc2!`C?KfbjuQmd_$9%){YG0MBq-71S_KF6zKyymSw*ZS9ekb6ZGyJiRdlAVoB-UUc'
    'AyCOMbqW^{95f!VXH3$0+D+$=qeE>*D?h;^h)$AgI18kthj4n6cuv^cTL~kj^J@GtRY(Ik<!(b5g&4eOGet>Ss}u-(0U85#ns88a'
    ';~M!4CLwKu$gxhx;2B84pHT{aFkPDJsC8)shq;dF2$6L4>aVmT_%LCEDqhmK$>NN!fwb;<>StYYr%4Dy<d0JZIY-fv_7hIZ6sSXB'
    '=GeJ=_gyN<E!RrbqzFNIiKY6JYIgobP#NT8t8g@mbABt(_}s>KlA?r@lZSy}q(xeg2OuT>Q;_%Ea1E4n!nn?v;eh^0NVykX1APl*'
    '=_TxR!-I4OZkqJM%3zF+SD-*%2N4**@_gvm6RDD82gVulIaj`m46sOFY~&Wdk&y8a?E!o9U#7o2cxKE8KnMTd!lHQ$f1(#>Tjdzx'
    'Q6^yk-eH!<3DCQa!2J`u$Z^X)VgM~=JKCZKkpF|RnfMO%2`m8iRR93W#NP@uysN?FSfEq64nCQnQCwgi8VCbzhCpwCHlBzTV9Pwy'
    'fOt$1ZFvqd*0((HD*WS$j6CVoOu!p$PMRq&_O+hJWr$(!AuXt=07>=nWO5{qv*<AtYr}A%AX4f6Gqrz<Mdwo@5(=E7Kp(_>%T(~&'
    'rhM7gF0Z0$B!s3%6GA;8;c3d<(6~Z0O&<@EhWmvwS1TRCw+j${AQ`P8t$~$JAF440#gZ-R1pqg#t*PTwpa$$OuNt$D=^&L51#ne='
    'eRzI)<>a(h|Ee)^p)uz$(M}wOl@y+EL}2>WhVZTB8b)C`4tn$UvLpx<)M=W2WYL>Om)mD?_`)AQdM?Y!FcOuY<d-TE7i%i+4p>ye'
    '?Z~dkVYX!Ryn)*oIjKy6Y6}tgHHNw~T%5o#oTPOY?#O~t$OLLz!qJ3doj5gAAt)?g_>N6>+t@*qtp~%#i1(>?(8w<>?<$<pVsGw+'
    '{C2Na(cZTzG*-r%vmDI<Fig;(XV*MkVzf$mm6(BRni9=v!L(@LEs(LrfRG%Cdq@1dvejGWm1cw=PuZ65mcrm04TPo_x;HO`ZeDPp'
    'ac{gga6fPL7WT(;H?FIIzY4+_IKL^fT%%n2aMG;J@*)8@It*^X#tgyZ-vVU@Nmgt<mIW)XW1*K#oX-vXvN2+S6eqFOq;;xZR-}MJ'
    '>+5@?$99D&)gnJWA?vIixcei`jVv8kHc~&f>JoP%s$x7~uFaL>kl9=y&=GJ<uicaY6AqX<qcyS}=(}wIGAPSnGRh<3B5{!?MLu%!'
    'cqlN+Y_d0cHYK`MNLxu-mqH!PlK^?AMmk9Hc5og^L<(6}qGeCWxXs=^L5zn7YTL1jz5$RxPoqPaqJ<$&*bj=XqNlmS6D(;_Em93o'
    '#9GGphKu}$#YGl1&vK(Op>*+dWH^=UD8jiFB9TF(mNmBgR#1rS(@G>%7~)!woCV_q+RSqs1o(3^hy)+JPDKD)xna)%HD9ySE)fjo'
    '9wlc`3CPqniAA|pG+sL1eN7A}qpwHS99m?Pd38gv7u+GH6aaHeG|pNyj?3b`naq2Y!<y=KDkq)AiHJ%>q4cO6hSte&60)085Q}Lh'
    '{BiZtq+3{H3Y+ZmpL?XbVWt~`A;FT2e$IG2X1WCnW>(v#{i1OLsf1j<@)RxH2jcmAL;m<eMsrmDf!7N;hz65XWZ>G)*!#beqg_U3'
    'uDr%GSGXdGYQDMr|L69PNwiCV8&aF=5Qc(12H@&pjnM$Lu2g!no(0$s?Kz_w09O_~4R=GG(2g@2=CVV#yG*AyF*7&k5?T$iI4~Vu'
    '62^l!5EY71>T!L$9HnMHlc6Es!(5x-A=c#Fp;LlIk#}*RG76eM+qK>2PaMhW6L%)PO8j*W`KnpN(q0m!qr%P5&g<8IVcZAT-l_tr'
    '6s-|nALO}`twQXhb2RyV%zi3ognh-4_~}UUc8E}YP$>bdV#!Q0QI?N!?&Ab*d;Hcff6~Vv%MicM&g^*T;pMs~?0a2rTg8|%U6}c?'
    'O47^}J)22-Y1;WDmQ1yzqMWmlBQ<G^GOITwk5wnM|0E6L3`$ywFXM$dyiqUzJ9G`B^3!7CDNdY-I0<EWC(1iDVJ5judaSbI*j+E6'
    '(yT;cpyfoi;z^Z}!k}Wh#k`-&12<$@nQL&wL}i6zguhb!wZva=I#)Vk2pCHvnl`zBO=8Kx&#=rK=2eT~N3|q=R14$BrT5UaE)cFU'
    'F_J5}GqQ?&|9o*j>Fb|bUTqM8*m)io?A>sd<Wza<7`LKA%m!UufTAS3Q{_D|!;G;`SqMnBZ5$_*egnG0(g6k(gc54aK7MSANUBLg'
    '(8)MszKM(LjdO4pMS$jOq+^ESWk1~LYRzU+u1c~MNMfJdbsiU{L0Ciq>6un0LzpxJHpP}Uh_wD&t5m!2C`iJ-Y6<eFm*|*O$_ilo'
    'V8Vx;vN@}L@vK_#$`ua^MKrFaC;Ow?AyOsSTx0Svy?;6V>Nmgr*WdlyzyAkcu6(h6@y*LWoWJraFVvW4-i}O5O!dOhDX0b$RXPr$'
    '+&JHqo&Ihz8A50#;2ueI!wApajW8f9-ws@(+Kp=5u=<tY>{H$&+h}KWsS)5Cg(hUa)3*-jAHvSZ6R{0wo)|S2&Sbv!0Tt`6;t+ds'
    'Kx7#PwRZHSCtlTi)LSa%UTJF7s@nI?O*e}EDbnK1{29SMt7=NwjxtSwrHlLLKf_;3{!^9;0j+mrvL2Js4L*1LTwCp#01EJX^Kq>j'
    '4O}q{iA(?VpdfgAoYryBA*ok)9I%^f_H&c=3B=W-W&IUAUgw(6sCm%=nn}-VUaue4?vHiV3AHB2tlT|0Rv4v5xh8?-VCD;H>hUZq'
    'wqj4zJw9f-KdO#UY=uE2p8xT?OQSsn>=Z0o1c@pO5wpBluW>t@d>gWygO>%C%$BFOeta)GHYwk(3I#WDsEQx$l3qhKG(&{_9>5c~'
    'i}>D*?%;aV>S%Y?==wn{36-%J6`!e#<62#JN6X4ycE-M?t#%Faem)r4N{>~y6u)%Z^%ZE{ZMMKeuCQ2@Yt~6P3V6ye+frNyQKYkf'
    'NbOE#cKSG-B;Czs#$J~%538ebi><JKSc|sk&~3-nxIa|1rf3YUCmF=2jH&AJkX=KZtT$;twdJfj)$*6A(>lI_KkKRAIAQNZCS;ux'
    'a8VYg-AY_?>GL)~?e8G%VIEvWm7<}*Vy&DSnZO-ay|D2ov;DH%9t+?zO8BbU`x*z^MJKh4-0Wy-AJFHh+iv2muejUot4%dhoR>k='
    '$4v&ABxSM&D|7OKQZ%{l4du-SDYJPN*k;Lt@N<YoVne!OfG>h*A?abEd#W2nv%g+RYDSC8@Sg0my^qsdo+3q0!BfXfaX{cNEv$t_'
    '|I(U4f1ii<e3wZAMA9ZOWj)L5$|<txfKh_d>LBaV5VkSiVUfy87Z7;dXV;*Fb^j%`42O5rFKpa((I(Bh5LqFKZV^Z}eB4p?&`lCu'
    '#UWO3p~_{Ja>dLUua)+%nY~%t`PzluxV3AdCCzRUEqQcvXK!%5Pq-u1J76K11m0(b&QgKK8FbG>_X?dFqXY4s?;WAlz%``-xT&|N'
    '-sm82ciG)BjWNv#<eY&`Lp4mgfxs<uruBLX6@c>|hF4YFDfX#%G`qhCHb;&$9aNB8#se>0rl*80RZkk`p@NUlfHUHr53<*8dN?Td'
    'GDZ(74>x%Kp|#)XC=n&gQw1Y33E@Dx4O529DLPD@;VP?b)byg|xS=jhKUl8?QQ%haNLz({Ew}v^;&Cju-aJIT^R+#a(JWedj&&_h'
    '+WLEHCSv6}%&U*j0!9TSp;`W$O_g;*el|xkC)cFIP{2f$Gl+LHnN;nBB(!d*W|BwOVtfi%nlN`2=V8KjeKgs0gbsiXcW~NM_^Hc*'
    'G=q?ww>l@P@_d<d27=K+DV2}7;~t!~gZ9-zf~TtuZkCFJU0vaHiAx!iDa+7}Y=LNtvmd4Zr5nsW;`1mP<;XkOOOQXRp`@w9=L2ev'
    'he`H-3<=*a=|7*u@0I+{Seh;OyONRaz^2jL5w{o6@+uiCUCGhkfpdDe<{*-93ndI`qpI0J*6kDhzK*%&1J!uv{FC_sW%>sQs)N7|'
    ')-swAs%)DFAyTZOzNho2GIa@$ZqcIF1Y8pf1N!2IU~#zk`t^&hZsFn%MPks*@nrPngx|h*Ep!FP!4jg7rs~xqm~*xk_xiqBysWir'
    'f)Cy@<V^qPB}J0#JZ61&8{~QQop;qa8#&pho(yn*h0=jO{igFZ%pNa!hRuAw;4)){X$qsLEs_nEQGu6YQl_yCV43CK{rEho-;`yD'
    'AgVr;)kkPU!piMjhuc{gn>g!|Zo}m9ur%l1+Q(V8dF5RVHs6k5Ts_(QXl@uE+K=@Q1UQwkFwWiQ4$0uxH43%_6^l%ANU@sf%je3%'
    '?jSEXb!n0(gb(`WKc<Xu^>diq*sbc0qQKC6dC|B+2K(Mtc*#d~+lNrsod3U08)VRT-*V8+mwV{MwhF~Y;e-qbsLQ$NG6j>+(B9l)'
    'd3YRsrm`>~v$rxI^R4*x(c0$<?wLI(N2o$Ai;b&D+5FHuh&M17b>vaAdX86Yn~A=*UNsqE!hIT7N}D&rPxZz7`X4w@sKTx5e<ncR'
    'AW7Dc^CIfLYmD5N4wsh!&YdCY##QYv!2JkA)jy7VO;T|e#6{L<+q3UIo{mc6sl4Z|_AfffP^mQ9Hr#h!QKzY++a5S+>$IAJn6tXL'
    'GAx5b7{Fg!Y78L+8`m{dx9^#dbhjBF<E~SrdBw5T@hiUUyy>*1etkyJN)$QRlGb^ZV(S@Yn{$mb=~m|cexS&kSeN0y#GBtWp_Ex3'
    '3}n+aMdm*kF&ccpkwLe{RRjMAP)h>@6aWAK2mte13|#;K0006200000000yK003%pZ*E_5WpH6>axZXsE_8WtWn=>YP)h>@6aWAK'
    '2mte13|-T-d;fU=006WA000*N003%pZ*E_5WpH6>axY(BX>MtBUtcb8c}pwG&sEUND$dV~FG?*)%q&vK%q_?-Dp5!SO2nsS=A`N<'
    '<m4x&K*(Yp1&FG6kPfaiu;$W|%$#Dq<ovwilEl1{VweG5!Tx?hsU@XFc_Ebrsa#y~@rgM(@$m|_3eie%!<BRtlt2c<=wc89$qEn`'
    ')gYx<E&xzV0|XQR000O8^I8mDz9|_ULI3~&LjV8(7XSbNYIARHUvgz|VQF$NUteuuX>MO%E^v8EE6UGR(90^$&x<cgElA8PQpn6L'
    '$S*2UNY2UR;>t`@h>y=p%uS7tSFp8JP>PSwP0Y-Tk5{tdQUC&=8VyY@08mQ<1QY-O00;o{S`1w~QT$S|0RRAJ0{{Ry0001Lb8l{6'
    'a%FH~X>u=cVRCb2UvqSFX>Mm<Xk~10WpZ;baA9(DWnW@%Z){~@ZeMS5Uv715Y%Xwll~YY`)G!de`&W#-ha_TIwUxL;-Cjz81!<|o'
    'UeGGC+-BT0c<t12SP<~v89VNN6mcMj*q%3U-kXV|yxT(f(P{etdfPc4VD<U-MqIzUx&E+NzGY0S(@4#L9u;}B*_Yl~(MjK<f1<zt'
    '^l^DRMqmQgJ<1wZi`N&gE*N7iDoE+#&N+k93g?9#j48(=HShzUSMc%*Vj}#4K6rXBoyF0>1TJ%lE6_bq9Grr%K`=U?mnJf(s~(Er'
    '2P~#gC~5k*aUP1OSwK11$H4_`t5oxTBR$L^dzn385HTcVD67cgyqX!eLtl>h-=rB(Y-LwNtM9{_6sHh|&fvJs)}!gBp~62CD?U8%'
    '8cH6W@hPlmsip@|B{10~^i5okVVmIC0im&Y27ez+)&H_VF7~e1vAKpNmL}jV?fM{npuq`kTl`MpJdJcr8M}=(I1WqNmZ+Ct(3Yw0'
    '`mCu^bev^Yqv%>U^}YoSFXnK*KOQTbBLDkE=(6gldgAAx>oE-8W>4q2^$9E#-P5p%|G>`quRZ1U7}8}LJ>8*l9y6WN*k7*a&*sLC'
    '<Tz)408mQ<1QY-O00;o{S`1zGo-`gr0RRA=0ssIw0001Lb8l{6a%FH~X>u=cVRCb2UvqSFX>Mm<Xk~10WpZ;baA9(DWnXG@Z*E^~'
    'Y;9j+Y;R+0E^v8;kx@^BFcgK~`zvm`mn~BsW()}lzD(3<oQv{ejDZS8>q?7lXPCd<Qos#oViQu@p7Wi1+d|0_%(i0U6<AqGWgvcB'
    'Maldsn%^vzmk1S&!t*wUaqVSNXhVe2z4-d!Zh245Mu~V^QGgK$`9T7R7w5Cn8A7ZW@&W{Qbj)`xMN$!^soLkQ+MNRy%4q>ko;WAx'
    'W%9yhwjQ^P0PxO^CvbfBtGqTGW9;UrHi~4n6(Vw*q)Nis7uC)KRAgLgngbJ%rfHLB3-+3SaGBa0z(x?OEf@&}vo!Lz$0=FYgKFE?'
    'Mm=?F>!dXVFj!8(e}*sw+!1lxgd;)NYxSB{iIGY6Mw*_e8siin0n6*qpX?0(L{Ba9AfDOp<fnwa_+|{XA^6Qr+{#aCPqgW8VPH}2'
    'IeO>rImd0C-SGwCnxbz|O9KQH000080P|W5T_T;H7li--0G<E<03HAU0BUn@ZeMa`aA9e3FLiWjY;!MTZ*FsRVQzGDE^v8EE6UGR'
    'D5)&S%u83u%q_?-Dp7FEtK{PH3ikI4N-ZfZ$_uG1NL8>^NXbkti7qZF(gDfGD%2?CWEPi1L)k!aAeokvpI8E<GxI=XQht68kn+pV'
    'OXUg<3G(!Fk9TqP@$~f!aSaN#f@lZoiv^k;t*)f5qoAa&1SB3c%z;pIm14O7P)h>@6aWAK2mte13|*^rjPqOp005=~001EX003%p'
    'Z*E_5WpH6>axZmsX>4;ZYIARHUt@1>bY*ySE^v93QqfAoFcf|7S6ueg!Zz@=pioBzrzq4pkTFWu-f5vtLXyr6{CJaW>yoW1-j{@&'
    'bI-l!khNe9AZ;6{8qS1*RNsd{TE&7OC~6`lOc+(TRdZ6eC__Vt;sA6#-%iuXEPqJf;M%r?k^4SR9$!-*NwO?)$j1p&$>?FqUb)0I'
    '<atFamFJ<vdL6;&2A&wjJ#NZ8kBdVW)Rj~VZ_}lovsj+dTOp8Yy2pesE3NtF@H?>KY8S(bv0CFZ(PGeTC{fwvhOjX@XR19b4+ewo'
    '{(=e!Q2d00hrP%+=opE3m#1D+mI0wZCWhGu`XZnhtPn_9V(AEGs9HhKG(F4if^NQJ!a341wCYiBraB#q_Qa>5J5oz0If2LQ{zxgq'
    'hHbPT?XY1|>Bhp$`YE#iIaFfjGd;_k@i6LvJG(A>F}gZ^HQW}+72MMjx2X_J#J|aS>s<tAf7~}vO9KQH000080P|W5T_O3s{a^wB'
    '0JH`G03-ka0BUn@ZeMa`aA9e3FLiWjY;!MfVrpe$bYEj{ZE#_7WpXZXd974UZ`&{oz57>i>0vRDWL=K|GHfWYV+Rbq4TYh~Osqwg'
    '42j8LU_X8omFCkevSB*dvPJPdzDH>pd<)cf#;w4#oeu<GTwfH0wkk&W_WTny-TSsv0mB^N2D+eDt(t@Lp#2s@0X%A4LRA@OsH(;h'
    '?XraFCwTA<=dzwgmlAS<2M@l(pvZU!JX!kCVhzRtHOM(#q_ENCLm^u~N8(W3A~n9=gPB-G)nes^;Fw<@=ZX0QczP#Oq44azJx~~p'
    'M*I19)SD2^7H!X<QeXu7<sqkxpj-{;;zs^AiVkU}@YtXUNjF|=73t<;ZH;~WkSpN~XF<n2%u@152_@IX!qWcFU3oxF;|9JeW7#QI'
    '%qU9S(|*=1bE&<1PiJ6h1Y{PDH`|ECF|>(>^;S-20u!i>rVG&f2K8D*U_q$?)=C1iNXZDW=vH(J%IQ-rrx15L2=F28*_Hin@$zwA'
    '@df+i1#X=rbm|@HfQy|>A*~Ayms{f^=1qiL|3I$*D&4?V*$ob%CdAXjGaG2xA588kx2#Odzht*tcnq5(SJ4jnYE8V(*A)0~z6!*D'
    '@%0VG4PiT^fwFddh2Z8zZu3-1`<T=iLAe!<_q5CMpeCO<j#A`XqqE&{zB_En22^#QkJ|*IKk?&Lk6ypfDDcU!eT8jD{aC=RyU7wl'
    '6o_PwY)m(aav-VFrAo>;3@~Pp>{LF7OdI(@o6cf6yGSY4h)&MC={`;53~QA8eFC?W95w+YJsV`W)8MFMgd>AffZsxgUx^RDm=HfR'
    'Bk9BBi^P|(Sio)Z2T)4`1QY-O00;o{S`1x+^m%|W2LJ%h8UO$&0001Lb8l{6a%FH~X>u=hbZKmJFLQKqX>Mm<W@&6?Uw3k0aByXE'
    'E^v9BSZ#0HHW2>qU%|N;AT6%z<fBmsXuGT(x@JI=ZXXQ8WhBaGEr|k2Cr!8g_Z?p(krHJ)F%rZPdCwj1<+&4SQ86wN6Qhi?0?EgU'
    'mK#LP(_8Xg{{H7^G)i(T1iFzN7Js8T`J3aaB78nV@Ogo5w^Shb!3cIFJ0uwr0Fl$|5y>q<2}{WY)^I5@#tZ!{1DKSEgwskgj<&c='
    'bFI1rX^e!{)jZaUQj($dnwC_q*O4H3HbGfBw^mM2vfY=vbxz8S+|CtFj9y-%YgQ6tr}~V?<L3MC07P+4e-Ld@?{w&a$}O0pEJco~'
    'V3N^=jIQaZfqcU^qE)6oS&AZKU<{VpbGvA8+6T}#^`zc?_;USm{m0ey``ZsFQb#euMj>JuA~@MvD*%BLoFWrItHL!(!pZLTk{mPb'
    'L7zMAn&_KmO>m5oOyb-ztu_I|UP@9*U;b{NWrPb9r8JRCK$)mmuK?>;ctBZ+`J+xC&`it;g+e`sxb7R)@s`<VAg95MP&qXPw=BRn'
    '&BGVytu+~Y21#&T!0{$MKp!|#M<(4T;7uxa2qy^<V(P22{+J`*A7H~j|1~>lhDs*A3`dOn|G!2Tetf;v1v+2PW;6BcoXp!&vIj6%'
    'pA-yN=PCEa9e4~5n`|-1iBxM<--pgYT753Iqw*smm7)Tm37acIVlGv&Yxh-5P?ZKK8YhhHC>h5HNW)Ed0VM;Gz@-B#2!REhk^E8D'
    'E!#_!LnsWTXgvi@Q&0d}F)H@Cq<4?fa1$t{ZdU2@tjz&xdmutN6*@vHILq0}-|p}Z`d70K>{5S54R>vbTpKT0_M&A<zPD5baSt=q'
    'X95*W?um+d&LHEkm(0VJ-=ia~(XA2lz(&FY)yZr0I*?$>o|hd|Ww=K30I)RStw=&M3gw`6g)0vd+*tA&&3tT*sUX1Buz1Be<I(t<'
    'Y_O#F)^;6Wq02+;E7i70I*wa|Z7D9iYMVkRA}t=fCfL0e&hH%B7JYcTZPZB@Qi?q%ovi69(YdZxYlFI3w$uWvE+tJ>en*XT3us)u'
    'X!a>Wiu&~!2dU(kr{8ICn<7w9kS5T>H&Bv|ASqM#IADP!c^-NCz=SAx*ciQDSokw^-pQ$uz*IG;GL5X(o)*0wPy>t<KyRqZnD9%8'
    '$IcwwQINT9#A?QKIOzsMk`|<3(9!N5(GHkTWov=Iql-^(y<!o%Z4|&%7xqDe1KZbi(_E<kq6@_wGP<Y<9lZ)z9*09<kz#b|0{hNk'
    'IW&K{-jPR8SVEhNsI;8b$j3gdWUkOp)}uaZ*KgB`RZ%O|0@hWgz3B{oP_tl1(ae4=djjE#oF?Q%7R7BlQ;dp@aHq{ZnNz=%kfFAL'
    'E4-D@uso>cIV9VNM-av^$u{c3Z2Q6`Yc;o*FxzmfhIPP$2fCZ)bf<jv`OT-f*JyjGKi4L37$0n>HDD~OC$RHRZ*8~~_*eQB6)fH7'
    '%$svVI)G4XCChJCPr~wjQPwR>X|1hRz?>9V3Ggts>NA3tK%`|t3_06y#lE-oP^(G_N}w*DxDyW`V_2Ov&1-5NfhD^AlUxvYp(D*I'
    '5N_2tAg~g|U{wc#D2Q`clnjWHYSb-QV{EVAwnsa2%<Bt-kpr@@;)w>mrg%($pP*SB@(gfcq`ppCq&RQ}<$^`X4%5y4!ug)qZf5FF'
    'i@@xtn&2HlKL}^zIEV{0b{@br>@*%vzcFg^Vo6teM5QYKb%U&rW)7gx;&>ncmkYh?fTB_8ZO2s8!h*aCb%2l<G)@rGjzurgd3=JT'
    '89Ja~8*M)y7hzQ+0h*2q*o-nVs-#pm)S{P0XE@Jl9Y?`^r!5?B^noocT3BahcR}KzaG6cZ#%c4~&Q+ie)+Qy7sB*wo9)_BNjv&C6'
    'c<I>SiktT#gP=x>Wfz0S-L&@n2qW9X@npOT&C2dM&zh?7RL8K;9HkvBfxhP5I1aocv|adYh^-F`_eWoQ|8#Kn4V0t#dulm8Darf<'
    'Gm1imwQ^@Bcx2}#JsZJv5}u9T6EjB)-Q^q`P86_n85F!Yu)~3Y8+ZwH=W^S~d_@mibxCk^TYLXeM`rjXBs`@Kyw5qHy@}Pa%8h|I'
    'I=4>Hz3NkQHRH9HmTD5JINlU^4zFx5_uLa6^xF*t@1&X5i@@=T*3Lybt^>m>oXx+C(+5!vhFd}Nz-)cqv|5i0?u-QzvqQt2GyUkj'
    'qtWrnr<r<M25Dcj_nebbHhD1ozT1Cc@2@>@NQg*#W<ap3FLn|toR}uJob){93j9h#nF0ENpk1k_>A@>G)eY40hTaqVf_CUq$JFZK'
    'w}01QFB%mm_a_vunqmTt_0SE=%kx$LE}(bCGC@pCZ?3LBu5Yex<Nn3!Q|??&x+r<hHAep`!*Sd3`;6z`w;aAn(j5-GvOE12XfO|+'
    'bSw2gP)h>@6aWAK2mte13|*??DS~+d001-u001li002}-Nm4{#P(e~uSzk;^Lq$$gMN=<ob8l`fa%FH~X>u)0Nkc_WQ$;RxcywJ-'
    'OOM(x5Wf3Yj5zH|(YD&&dNYYxtmc6nvy`Jr9O5d(C^kj=?>iIN-L?`^<avCrp^zGK*Vr*w#!`QNo7}OF@X$QM^VjEZP)FOD(ZkX-'
    'jWeSi2E*9KPA3<F^CP$by`Ajg4UUr;qiqxDCTqcW(DY_H+61BpGrqyuP5~GEAsRQj@dySQ<YPD)dxQzTi&ryQ9B#pc;E@mUp!Ln='
    'WXEVC@pW#n0Uly+VNu^V7LPMYYt6vi2-JIT!PUjyUm{F4#K|>;k$@YU;nI?Z_x9jU?#@8#a|nSU=p~>7(wV@?x2~hFoo$^jhrxwD'
    'fz}c9;S#YJs5qyYkdH5Zf`FMZL~y8k_WG%Ao+0UH3N+p$3RJoF{`9XC7g%?hMx?a!PU|tUnemrxB2{R-^TXh;WUcXI>&Q{~&X~rQ'
    'Ir!gp_Hw)N=p#zKNuUFqKd*Xkh29JU9PB+X<i&W+Uv?&<4-pUV%m8OU&3ymcd_w-UfVx`g9ajQm4I5Rx%1mUi;5EJ%3GAd^S6dA@'
    'p?InHP_2NMd-x&CECKOzqeNXprI;)>xfEEFWtwj@S-!v$_sR<Y$g+?cL3IU0c!!drCa^-Nbd68Gl)2RVgsr4533F8`;IQFJ%XFJ_'
    '1)HtfRJB0*3}H)It`xF}LX`RmS+NG<6(3Nqd7cv$<6Bg(NFSutX0PPSTEn`^Gl9jWKxKTHiyIZ1N^>rY1TtRm7cp;Dh@u#c-PFNu'
    'EvQ6%9Dk{nRY^9|s?-Xf3A$AJV|6EMkpNe+ra)GzDiTI<;zl)t;9e<iC=@mPa}gY&_qG-vfRG8EBUVkzWbu9YiTwdkO928D02BZK'
    '00;m8Tnt^3(`}wy1poj^6aWAm00000000000001h0RR910BLS!WpZV1V`VR4ZeeVBdSzd8WpZ|DV`VOIc~DCM0u%rg00008003MJ'
    'UHX04+Ncu%0QNZm03HAU00000000000Du9O1pokPZf0e2Wo~0-FJ*RNY;|FDX>V>{WMy<^V{|TXc~DCM0u%rg00008003MJUF3^}'
    'IZ&Yh07QHO02=@R00000000000Du8{82|ujZf0e2Wo~0-FK}{iW@&6?Uu0!;Wn*+MaCuNm0Rj{N6aWAK2mk<F3|)mDFa^5;0027$'
    '0012T0000000000004ji1G@kKX>Mj^a%FB~WiN1YZ*6dNb1z?CX>MtBUtcb8c~DCM0u%rg00008003MJT^d)l#J&Ik0Nwxq03-ka'
    '00000000000DuAcy#N4dZf0e2Wo~0-FK}{iZE$pRFJobCaCLJpUtei%X>?y-E^v8JO928D02BZK00;m8Tnt^%&bZw$82|uSKL7wG'
    '00000000000001h0r|cF0BLS!WpZV1V`VRJa&K*LbaO9bVQp}Ab1!9fVQh6_bZKvHE^v8JO928D02BZK00;m8Tnt@mDT_v41polv'
    '2mk;j00000000000001h0d3d-0BLS!WpZV1V`VRJa&K*LbaO9bVQp}Ab1!LbbY*gOX=QgVaCuNm0Rj{N6aWAK2mk<F3|(GkQBoNT'
    '008|S001HY0000000000004ji4&DF&X>Mj^a%FB~WiN1YZ*6dNb1!3IZE$sSFLGsbb!}xXaCuNm0Rj{N6aWAK2mk<F3|%9bajhH%'
    '006NJ001KZ0000000000004jiXXyX{X>Mj^a%FB~WiN1YZ*6dNb1!3IZE$sSFLPsWa%pa7E^v8JO928D02BZK00;m8Tnt@{rNL8q'
    '0001g0000c00000000000001h0mSbB0BLS!WpZV1V`VRJa&K*LbaO9hZggdGZeeUMUtei%X>?y-E^v8JO928D02BZK00;m8Tnt?Z'
    'ryqyP7ytlrJ^%nH00000000000001h0fF!U0BLS!WpZV1V`VRJa&K*LbaO9hZggdGZeeUMWp-g~bzyXAZ*DGdc~DCM0u%rg00008'
    '003MJT_HsB<Rt|F0DlMo04D$d00000000000Du9J2>}3UZf0e2Wo~0-FK}{iZE$pRFKKRcWpZv|Y%ghUbY*gOX=QgVaCuNm0Rj{N'
    '6aWAK2mk<F3|&a`b>+_r005{S001Na0000000000004ji`wjsBX>Mj^a%FB~WiN1YZ*6dNb1!LbbY*gGVQepQWpj0HWiD`eP)h*<'
    '6aW+e000O809*`RLQ*XG-~|8xS_}XHB>(^b0000000000fB_5|0RU-kW@U0^ZewLHaB^>LaCCDoX>N37a&BR4FLPsWa%pa7E^v8J'
    'O928D02BZK00;m8Tnt?^$aJb42mk<-4*&og00000000000001h0WTl{0BLS!WpZV1V`VRJa&K*LbaO9rXkl_?WG--dP)h*<6aW+e'
    '000O809*`R9b!K+H4^{;lR^Lh8~^|S0000000000fB}Rk0RU-kW@U0^ZewLHadl;LbZKvHUu0!;Wn*+MaCuNm0Rj{N6aWAK2mk<F'
    '3|&0|`dv!{008O+0015U0000000000004ji^E&|mX>Mj^a%FB~WiNAda$$63XL&DQUukY>bYEXCaCuNm0Rj{N6aWAK2mk<F3|(=j'
    ')ly6d008zF000^Q0000000000004jidOraGX>Mj^a%FB~WiNAda$$63XL&DTVRL0JaCuNm0Rj{N6aWAK2mk<F3|&Eq?3+p>005L-'
    '000~S0000000000004ji{zm}-X>Mj^a%FB~WiNAda$$63XL&DUVQp}Ab1rasP)h*<6aW+e000O809*`R8X#i@4I%&l7g+!R9smFU'
    '0000000000fB}MQ0RU-kW@U0^ZewLHb98cHbY*9GFKKRcWpZv|Y%XwlP)h*<6aW+e000O809*`RLbk<3vk?FQv^W3&8~^|S00000'
    '00000fC0;m0RU-kW@U0^ZewLHcV%H~Ze??GUu0!;Wn*+MaCuNm0Rj{N6aWAK2mk<F3|&-Tp?PQw000jn001KZ0000000000004ji'
    'xSs(4X>Mj^a%FB~WiNMca%*2@cyMKMX=QF>WnW}vbY)|7E^v8JO928D02BZK00;m8Tnt@*k|9`I3IG7H9RL6k00000000000001h'
    '0b#5G0CjX}Y%g?WcywQNbZKlZaCuNm0Rj{N6aWAK2mk<F3|$zOD3==n001!p000;O0000000000004ji>$U*^WMOn+FJ^UaV_#%t'
    'aCBd8VQpnDYIARHP)h*<6aW+e000O809*`Re6OBIZ2$lO?EnA(9{>OV0000000000fB`(X0RUuSbYU-dZ*ps2b97;JX>V>{WNC6`'
    'V{~tFE^2dcZcs}B0u%rg00008003MJT>t<800IC20000001yBG00000000000DuAJxB&olbZKlaUtei%X>?y-E^v8JO928D02BZK'
    '00;m8Tnt?R00002000000000G00000000000001h0T#Id0AyiwVJ}}_X>MtBUtcb8c~DCM0u%rg00008003MJT>t<800IC200000'
    '02KfL00000000000Du8Txd8xdVQyz*Xklq?FJE72ZfSI1UoLQYP)h*<6aW+e000O809*`R-4YSu2mt^95&{4K2><{90000000000'
    'fB}2C0RU`oXJ=({E^v8JO928D02BZK00;m8Tnt@CWWHz_0RRAC0ssIL00000000000001h0jjzI0CjX}Y%gPPZf0p`Uv+e8Y%Xwl'
    'P)h*<6aW+e000O809*`R@P|?ZDFFZg>H+`&7XSbN0000000000fC2Qo0RVM$X>2cYWpj0GbYEj(Y;0m-V{0yOc~DCM0u%rg00008'
    '003MJU2H`O8^Ztq0CNEV0384T00000000000Du8fy#WAhVQyz*Xklq?FK}{iZE$pRFJE72ZfSI1UoLQYP)h*<6aW+e000O809*`R'
    'TTB_u$OixbWfTAa7ytkO0000000000fB{gx0RU-kW@U0^ZewLHad%~IUt?@(Wo~pXaCuNm0Rj{N6aWAK2mte13|;Ymn(<o-001u`'
    '000^Q0000000000006duPR0QMYIARHUvgz|VQF$NYIARHUvOb^b7gWaaCuNm0Rj{N6aWAK2mte13|&r?yW%Sh007o8000^Q00000'
    '00000006du;m`p9YIARHUvgz|VQF$NYIARHUvgz|VQF$MaCuNm0Rj{N6aWAK2mte13|-K&uuqo*006%T000^Q0000000000006du'
    'MBM=ZYIARHUvgz|VQF$NaA9(DWnW=(a$$KcaCuNm0Rj{N6aWAK2mte13|+`mL*ga_003kP000~S0000000000006du6X5{>YIARH'
    'Uvgz|VQF$NaA9(DWnW`&ZEa<4bS`jtP)h*<6aW+e000O8^I8mDJ{&|(F9HAnDFpxk8vp<R0000000000wt;ix0RU=qZ*E_5WpH6>'
    'axZXUa&u*0Zgp*9WpXZXc~DCM0u%rg000080P|W5T?TGKjk5;;07@GG02=@R00000000000JeeK<^cd|b8l{6a%FH~X>u=cVRCb2'
    'UvFY+Wn*+MaCuNm0Rj{N6aWAK2mte13|;os=lB#9001Ip000{R0000000000006du$L;|DYIARHUvgz|VQF$NaA9(DWnXi2a%pa7'
    'E^v8JO928D02BZK00;o{S`1wP00002000000000K00000000000002CffNG*0BUn@ZeMa`aA9e3FK~G-ba`-PWKc^10u%rg00008'
    '0P|W5UDLFC|9Joa0JH!A02crN00000000000Jech0|EeQb8l{6a%FH~X>u=LUukY>bYEXCaCuNm0Rj{N6aWAK2mte13|+n{8683Z'
    '002V(000*N0000000000006du_5%U{YIARHUvgz|VQF$NUteuuX>MO%E^v8JO928D02BZK00;o{S`1w~QT$S|0RRAJ0{{Ry00000'
    '000000002Cfo%i=0BUn@ZeMa`aA9e3FK}UUb7fz1baH8KXJ2S#Y;a|Ab1!gVa&u*0VsCG3Wnpe#Z*pI5b!==daCuNm0Rj{N6aWAK'
    '2mte13|;n~G#*3&005o>001}u0000000000006dubp`?eYIARHUvgz|VQF$NaA9(DWnXi2a%pa7Uub1)aAk6HFK}UUb7fy@b8l{6'
    'Y;0{`Vr*|?Yc6nkP)h*<6aW+e000O8^I8mDBAuQWg#Z8mo&W#<9smFU0000000000wt)`_0sv}rZ*E_5WpH6>axZmsX>4;ZV{dMA'
    'bYX6Eb1rasP)h*<6aW+e000O8^I8mDt96X?Tmb+8rUL)~ApigX0000000000wt>(H0sv}rZ*E_5WpH6>axZmsX>4;ZYIARHUt@1>'
    'bY*ySE^v8JO928D02BZK00;o{S`1ww`Mv#M0ssKC1^@sg00000000000002Cfolo^0BUn@ZeMa`aA9e3FLiWjY;!MfVrpe$bYEj{'
    'ZE#_7WpXZXc~DCM0u%rg000080P|W5U4ry^fH4OE0L~f!04M+e00000000000Jeb*4FUjab8l{6a%FH~X>u=hbZKmJFLQKqX>Mm<'
    'W@&6?Uw3k0aByXEE^v8JO928D02BZK00;o{S`1yP;wgf80ssIs1ONam00000000000002CfrJzS08~gxQbb=+K~hv%Urb3uMNU&i'
    'Q!i?BZ*DDeWpH6>axF|rLq$$gMJ{xBbWlqL1^@s605SkF0MZTs07n-B0000'
)
# cspell:enable


def _env(name: str, default: Any) -> Any:
    value = os.getenv(name)
    return default if value in (None, "") else value


def _safe_name(value: str) -> str:
    import re
    return re.sub(r"[^0-9A-Za-z._-]+", "_", value).strip("_") or "model"


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_step_csv(path: Path, steps: list[dict[str, Any]]) -> None:
    fields = [
        "concurrency", "rounds", "attempted_requests", "sent_requests", "completed_requests", "success_count", "failed_count",
        "success_rate_percent", "error_rate_percent", "wall_clock_seconds",
        "measurement_duration_seconds", "active_burst_duration_seconds",
        "sent_requests_per_second", "attempted_requests_per_second", "successful_requests_per_second",
        "active_burst_total_qps", "active_burst_success_qps",
        "gateway_attempt_qps", "gateway_success_qps",
        "business_average_ms", "business_p95_ms", "business_p99_ms",
        "business_p95_observed_ms", "business_p99_observed_ms", "business_p95_reliable", "business_p99_reliable",
        "business_latency_samples", "all_request_latency_samples", "gateway_ttfb_samples",
        "network_total_average_ms", "network_total_p95_ms", "network_total_p99_ms",
        "all_network_total_average_ms", "all_network_total_p95_ms", "all_network_total_p99_ms",
        "response_transfer_average_ms", "response_transfer_p95_ms", "response_transfer_p99_ms",
        "all_response_transfer_average_ms", "all_response_transfer_p95_ms", "all_response_transfer_p99_ms",
        "ttfb_average_ms", "ttfb_p95_ms", "ttfb_p99_ms", "gateway_call_count",
        "gateway_success_count", "gateway_failure_count", "gateway_error_rate_percent",
        "observed_peak_business_requests", "observed_peak_gateway_calls",
        "effective_duration_seconds", "total_qps", "success_qps",
        "response_avg_ms", "response_min_ms", "response_max_ms", "response_p50_ms", "response_p90_ms",
        "response_p95_ms", "response_p99_ms", "all_request_avg_ms", "all_request_p95_ms", "all_request_p99_ms",
        "all_request_p95_observed_ms", "all_request_p99_observed_ms",
        "prompt_tokens", "completion_tokens", "total_tokens",
        "prompt_usage_coverage_percent", "completion_usage_coverage_percent", "total_usage_coverage_percent",
        "input_token_throughput", "output_token_throughput", "total_token_throughput",
        "gateway_input_token_throughput", "gateway_output_token_throughput", "gateway_total_token_throughput",
        "average_output_tokens_per_successful_request", "full_concurrency_bursts",
        "burst_results", "metric_quality_warnings", "error_summary",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for step in steps:
            business = step["business_wall_elapsed_ms"]["successful_requests"]
            network = step["complete_network_transfer_total_ms"]
            transfer = step["response_completion_first_to_last_byte_ms"]
            ttfb = step["gateway_call_metrics_ms"]["time_to_first_byte"]
            writer.writerow({
                "concurrency": step["concurrency"],
                "rounds": step["rounds"],
                "attempted_requests": step["attempted_requests"],
                "sent_requests": step["sent_requests"],
                "completed_requests": step["completed_requests"],
                "success_count": step["success_count"],
                "failed_count": step["failed_count"],
                "success_rate_percent": step["success_rate_percent"],
                "error_rate_percent": step["error_rate_percent"],
                "wall_clock_seconds": step["wall_clock_seconds"],
                "measurement_duration_seconds": step["measurement_duration_seconds"],
                "active_burst_duration_seconds": step["active_burst_duration_seconds"],
                "sent_requests_per_second": step["throughput"]["sent_requests_per_second"],
                "attempted_requests_per_second": step["throughput"]["attempted_requests_per_second"],
                "successful_requests_per_second": step["throughput"]["successful_requests_per_second"],
                "active_burst_total_qps": step["throughput"]["active_burst_total_qps"],
                "active_burst_success_qps": step["throughput"]["active_burst_success_qps"],
                "gateway_attempt_qps": step["throughput"]["gateway_attempt_qps"],
                "gateway_success_qps": step["throughput"]["gateway_success_qps"],
                "business_average_ms": business["average"],
                "business_p95_ms": business["p95"],
                "business_p99_ms": business["p99"],
                "business_p95_observed_ms": business["p95_observed"],
                "business_p99_observed_ms": business["p99_observed"],
                "business_p95_reliable": business["p95_reliable"],
                "business_p99_reliable": business["p99_reliable"],
                "business_latency_samples": business["sample_count"],
                "all_request_latency_samples": step["business_wall_elapsed_ms"]["all_requests"]["sample_count"],
                "gateway_ttfb_samples": ttfb["sample_count"],
                "network_total_average_ms": network["average"],
                "network_total_p95_ms": network["p95"],
                "network_total_p99_ms": network["p99"],
                "all_network_total_average_ms": step["all_request_network_transfer_total_ms"]["average"],
                "all_network_total_p95_ms": step["all_request_network_transfer_total_ms"]["p95"],
                "all_network_total_p99_ms": step["all_request_network_transfer_total_ms"]["p99"],
                "response_transfer_average_ms": transfer["average"],
                "response_transfer_p95_ms": transfer["p95"],
                "response_transfer_p99_ms": transfer["p99"],
                "all_response_transfer_average_ms": step["all_request_response_completion_first_to_last_byte_ms"]["average"],
                "all_response_transfer_p95_ms": step["all_request_response_completion_first_to_last_byte_ms"]["p95"],
                "all_response_transfer_p99_ms": step["all_request_response_completion_first_to_last_byte_ms"]["p99"],
                "ttfb_average_ms": ttfb["average"],
                "ttfb_p95_ms": ttfb["p95"],
                "ttfb_p99_ms": ttfb["p99"],
                "gateway_call_count": step["traffic"]["gateway_call_count"],
                "gateway_success_count": step["errors"]["gateway_success_count"],
                "gateway_failure_count": step["errors"]["gateway_failure_count"],
                "gateway_error_rate_percent": step["errors"]["gateway_error_rate_percent"],
                "observed_peak_business_requests": step["concurrency_observation"]["observed_peak_business_requests"],
                "observed_peak_gateway_calls": step["concurrency_observation"]["observed_peak_gateway_calls"],
                "effective_duration_seconds": step["effective_duration_seconds"],
                "total_qps": step["throughput"]["total_qps"],
                "success_qps": step["throughput"]["success_qps"],
                "response_avg_ms": business["average"],
                "response_min_ms": business["min"],
                "response_max_ms": business["max"],
                "response_p50_ms": business["p50"],
                "response_p90_ms": business["p90"],
                "response_p95_ms": business["p95"],
                "response_p99_ms": business["p99"],
                "all_request_avg_ms": step["business_wall_elapsed_ms"]["all_requests"]["average"],
                "all_request_p95_ms": step["business_wall_elapsed_ms"]["all_requests"]["p95"],
                "all_request_p99_ms": step["business_wall_elapsed_ms"]["all_requests"]["p99"],
                "all_request_p95_observed_ms": step["business_wall_elapsed_ms"]["all_requests"]["p95_observed"],
                "all_request_p99_observed_ms": step["business_wall_elapsed_ms"]["all_requests"]["p99_observed"],
                "prompt_tokens": step["token_usage"]["total_prompt_tokens"],
                "completion_tokens": step["token_usage"]["total_completion_tokens"],
                "total_tokens": step["token_usage"]["total_tokens"],
                "prompt_usage_coverage_percent": step["token_usage"]["prompt_usage_coverage_percent"],
                "completion_usage_coverage_percent": step["token_usage"]["completion_usage_coverage_percent"],
                "total_usage_coverage_percent": step["token_usage"]["total_usage_coverage_percent"],
                "input_token_throughput": step["token_usage"]["input_token_throughput"],
                "output_token_throughput": step["token_usage"]["output_token_throughput"],
                "total_token_throughput": step["token_usage"]["total_token_throughput"],
                "gateway_input_token_throughput": step["token_usage"]["gateway_input_token_throughput"],
                "gateway_output_token_throughput": step["token_usage"]["gateway_output_token_throughput"],
                "gateway_total_token_throughput": step["token_usage"]["gateway_total_token_throughput"],
                "average_output_tokens_per_successful_request": step["token_usage"]["average_output_tokens_per_successful_request"],
                "full_concurrency_bursts": step["full_concurrency_bursts"],
                "burst_results": json.dumps(step["burst_measurements"], ensure_ascii=False),
                "metric_quality_warnings": json.dumps(step["metric_quality_warnings"], ensure_ascii=False),
                "error_summary": json.dumps(step["errors"]["error_summary"], ensure_ascii=False),
            })


def _package_bytes() -> bytes:
    payload = base64.b85decode(BUSINESS_PACKAGE_B85.encode("ascii"))
    digest = hashlib.sha256(payload).hexdigest()
    if digest != BUSINESS_PACKAGE_SHA256:
        raise RuntimeError(f"内嵌业务包校验失败：expected={BUSINESS_PACKAGE_SHA256}, actual={digest}")
    return payload


def _runtime_dir() -> Path:
    return Path(tempfile.gettempdir()) / f"ai_interview_single_{BUSINESS_PACKAGE_SHA256[:16]}"


def _verify_original_sources(runtime: Path) -> None:
    failures = []
    for relative, expected in ORIGINAL_SOURCE_HASHES.items():
        path = runtime / relative
        if not path.is_file():
            failures.append(f"缺失 {relative}")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            failures.append(f"哈希不符 {relative}")
    if failures:
        raise RuntimeError("研发核心源码校验失败：" + "；".join(failures))


def _extract_business_package() -> Path:
    runtime = _runtime_dir()
    ready = runtime / ".ready"
    if ready.is_file() and ready.read_text(encoding="ascii", errors="ignore") == BUSINESS_PACKAGE_SHA256:
        _verify_original_sources(runtime)
        return runtime

    parent = runtime.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="ai_interview_unpack_", dir=parent))
    try:
        with zipfile.ZipFile(io.BytesIO(_package_bytes())) as archive:
            for info in archive.infolist():
                destination = (staging / info.filename).resolve()
                if staging.resolve() not in destination.parents and destination != staging.resolve():
                    raise RuntimeError(f"业务包包含非法路径：{info.filename}")
            archive.extractall(staging)
        _verify_original_sources(staging)
        (staging / ".ready").write_text(BUSINESS_PACKAGE_SHA256, encoding="ascii")
        if runtime.exists():
            shutil.rmtree(runtime)
        staging.replace(runtime)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return runtime


def _load_business_runtime(config: dict[str, Any]):
    runtime = _extract_business_package()
    runtime_text = str(runtime)
    if runtime_text not in sys.path:
        sys.path.insert(0, runtime_text)

    # 这些模块由上面的内嵌业务包在运行时释放，因此使用动态导入，避免 IDE
    # 在脚本尚未运行时将它们误报为“未解析的引用”。
    qwen_client: Any = importlib.import_module("inference.qwen_client")
    config_util: Any = importlib.import_module("util.config_util")
    callbacks: Any = importlib.import_module("util.result_callback")

    qwen_client.set_gateway_config(
        customer_code=config["customer_code"], secret_key=config["secret_key"],
        url=config["url"], model=config["model"], component_code=config["component_code"],
        timeout=config["timeout"], verify_ssl=config["verify_ssl"],
        max_tokens=config.get("max_tokens"),
    )
    for option, value in (
        ("model_name", config["model"]), ("api_url", config["url"]),
        ("app_key", config["customer_code"]), ("secret_key", config["secret_key"]),
        ("component_code", config["component_code"]),
    ):
        config_util.set_config_value("qwen", option, value)
    _install_callback_capture(callbacks)
    return runtime, qwen_client, callbacks


def fixture_for(recruit: str, task: str) -> dict[str, Any]:
    try:
        data = json.loads(EMBEDDED_FIXTURES_JSON[(recruit, task)])
    except KeyError as exc:
        raise ValueError("没有内置数据：{}/{}".format(recruit, task)) from exc
    if not isinstance(data, dict):
        raise ValueError(f"内置数据不是 JSON object：{recruit}/{task}")
    return data


def _load_fixture(path: str | None, recruit: str, task: str) -> dict[str, Any]:
    if not path:
        return fixture_for(recruit, task)
    fixture_path = Path(path).expanduser().resolve()
    data = json.loads(fixture_path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"fixture 必须是 JSON object：{fixture_path}")
    return data


def _resume_id(data: dict[str, Any]) -> Any:
    for key in ("resumeId", "resumeid", "resume_id", "id"):
        if data.get(key) not in (None, ""):
            return data[key]
    for nested_key in ("resumeInfo", "resume"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for key in ("resumeId", "resumeid", "resume_id", "id"):
                if nested.get(key) not in (None, ""):
                    return nested[key]
    return "modeltest"


def _adapt_request(data: dict[str, Any], recruit: str, task: str) -> tuple[Any, dict[str, Any], int]:
    """只做 HTTP 入参形状适配；原 fixture 对象本身不修改。"""
    body = json.loads(json.dumps(data, ensure_ascii=False))
    recruit_type = 1 if recruit == "campus" else 31
    resume_id = _resume_id(body)
    body["recruitType"] = recruit_type

    if task == "profile":
        if not isinstance(body.get("resumeInfo"), dict):
            body = {"resumeInfo": body, "recruitType": recruit_type}
    elif task == "questions":
        if not isinstance(body.get("resume"), dict) and isinstance(body.get("resumeInfo"), dict):
            body["resume"] = body["resumeInfo"]
    elif task == "report":
        body.setdefault("resumeId", resume_id)
        if "dimension" not in body and "dimensions" in body:
            body["dimension"] = body["dimensions"]
    return resume_id, body, recruit_type


def _public_config(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "customer_code": config["customer_code"], "url": config["url"],
        "model": config["model"], "component_code": config["component_code"],
        "timeout": config["timeout"], "verify_ssl": config["verify_ssl"],
        "has_secret_key": bool(config["secret_key"]),
    }


def _ping(qwen_client, timeout: int) -> dict[str, Any]:
    started = time.perf_counter()
    response = qwen_client.qwen_request(
        '只输出一个JSON对象：{"ping":"ok"}，不要解释。',
        max_retries=0, timeout=min(int(timeout), 120), rid="modeltest-ping",
        temperature=0.0,
    )
    if not response:
        raise RuntimeError(qwen_client.get_last_error() or "网关返回为空")
    return {"ok": True, "elapsed_sec": round(time.perf_counter() - started, 6), "response_preview": response[:300]}


_NETWORK_CONTEXT = threading.local()


def _round_metric(value: float | None) -> float | None:
    return None if value is None else round(float(value), 6)


def _format_ms(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.6f}ms"


@contextmanager
def _suppress_business_logs():
    """Keep model prompts and response bodies out of performance-test stdout/stderr."""
    previous_level = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(previous_level)


def _percentile(values: list[float], percentile: float) -> float | None:
    """Reference-script percentile: linear interpolation on sorted samples."""
    if not values:
        return None
    if not 0 <= percentile <= 100:
        raise ValueError("percentile 必须在 0 到 100 之间")
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (percentile / 100)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _find_usage(data: Any) -> dict[str, Any] | None:
    if isinstance(data, dict):
        usage = data.get("usage")
        if isinstance(usage, dict):
            return usage
        for value in data.values():
            found = _find_usage(value)
            if found is not None:
                return found
    elif isinstance(data, list):
        for value in data:
            found = _find_usage(value)
            if found is not None:
                return found
    return None


def _first_usage_int(usage: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = _to_int(usage.get(key))
        if value is not None:
            return value
    return None


def _extract_token_usage(data: Any) -> tuple[int | None, int | None, int | None]:
    usage = _find_usage(data) or {}
    prompt = _first_usage_int(usage, ("prompt_tokens", "input_tokens", "prompt_token_count"))
    completion = _first_usage_int(
        usage,
        ("completion_tokens", "output_tokens", "generated_tokens", "completion_token_count"),
    )
    total = _first_usage_int(usage, ("total_tokens", "total_token_count"))
    if total is None and prompt is not None and completion is not None:
        total = prompt + completion
    if completion is None and prompt is not None and total is not None:
        completion = max(total - prompt, 0)
    if prompt is None and completion is not None and total is not None:
        prompt = max(total - completion, 0)
    return prompt, completion, total


def _find_business_error(data: Any) -> str:
    if isinstance(data, list):
        for item in data:
            error = _find_business_error(item)
            if error:
                return error
        return ""
    if not isinstance(data, dict):
        return ""
    if data.get("error"):
        return f"Business error: {str(data['error'])[:200]}"
    if data.get("success") is False:
        return "Business success=false"
    for key in ("code", "status_code", "error_code"):
        if key not in data:
            continue
        value = data[key]
        numeric = _to_int(value)
        if numeric is not None and numeric not in (0, 200):
            return f"Business {key}: {value}"
        if numeric is None and str(value).strip().lower() not in {"", "0", "200", "ok", "success", "succeeded"}:
            return f"Business {key}: {value}"
    if str(data.get("status", "")).strip().lower() in {"error", "failed", "failure", "fail"}:
        return f"Business status: {data.get('status')}"
    for value in data.values():
        error = _find_business_error(value)
        if error:
            return error
    return ""


def _normalize_error(error: str | None, category: str | None = None) -> str:
    text = " ".join(value for value in (category or "", error or "") if value).lower()
    if "timeout" in text or "timed out" in text:
        return "Timeout"
    if "connection" in text or "urlerror" in text or "connect" in text:
        return "Connection error"
    if "http" in text or "status" in text:
        return "HTTP error"
    if "business" in text or "callback" in text:
        return "Business error"
    if "empty" in text:
        return "Empty output"
    return (error or "Unknown")[:80]


def _short_error(value: Any, limit: int = 240) -> str:
    """Keep backend error output readable while retaining the useful prefix."""
    text = " ".join(str(value or "Unknown").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _request_error_text(item: dict[str, Any]) -> str:
    for call in item.get("network_calls", []):
        for key in ("business_error", "response_error_preview"):
            if call.get(key):
                return _short_error(call[key])
    if item.get("error"):
        return _short_error(item["error"])
    for call in item.get("network_calls", []):
        if call.get("error"):
            return _short_error(call["error"])
    return _short_error(item.get("error_category") or "Unknown")


def _failed_request_records(results: list[dict[str, Any]], concurrency: int) -> list[dict[str, Any]]:
    records = []
    for item in results:
        if item.get("success"):
            continue
        calls = []
        for call in item.get("network_calls", []):
            calls.append({
                "status_code": call.get("status_code"),
                "error": call.get("error"),
                "business_error": call.get("business_error"),
                "response_error_preview": call.get("response_error_preview"),
                "response_error_body": call.get("response_error_body"),
                "response_error_body_truncated": call.get("response_error_body_truncated", False),
            })
        records.append({
            "concurrency": concurrency,
            "burst_id": item.get("burst_id"),
            "request_number": item.get("request_number"),
            "resume_id": item.get("resume_id"),
            "sent": bool(item.get("sent")),
            "error_category": item.get("error_category"),
            "error": item.get("error"),
            "error_summary": _request_error_text(item),
            "status_code": item.get("status_code"),
            "network_calls": calls,
        })
    return records


def _gateway_failure_records(results: list[dict[str, Any]], concurrency: int) -> list[dict[str, Any]]:
    records = []
    for item in results:
        for call_index, call in enumerate(item.get("network_calls", []), start=1):
            gateway_success = (
                isinstance(call.get("status_code"), int)
                and 200 <= call["status_code"] < 300
                and call.get("error") is None
            )
            if gateway_success:
                continue
            backend_error = (
                call.get("business_error")
                or call.get("response_error_preview")
                or call.get("error")
                or f"HTTP status {call.get('status_code')}"
            )
            records.append({
                "concurrency": concurrency,
                "burst_id": item.get("burst_id"),
                "request_number": item.get("request_number"),
                "resume_id": item.get("resume_id"),
                "gateway_call_index": call_index,
                "business_request_success": bool(item.get("success")),
                "status_code": call.get("status_code"),
                "error": call.get("error"),
                "business_error": call.get("business_error"),
                "response_error_preview": call.get("response_error_preview"),
                "response_error_body": call.get("response_error_body"),
                "response_error_body_truncated": call.get("response_error_body_truncated", False),
                "error_summary": _short_error(backend_error),
            })
    return records


def _write_concurrency_errors(
    path: Path,
    recruit: str,
    task: str,
    failed_business_requests: list[dict[str, Any]],
    failed_gateway_calls: list[dict[str, Any]],
) -> None:
    _write_json(path, {
        "test_case": {"recruit": recruit, "task": task},
        "failed_business_request_count": len(failed_business_requests),
        "failed_gateway_call_count": len(failed_gateway_calls),
        "failed_business_requests": failed_business_requests,
        "failed_gateway_calls": failed_gateway_calls,
    })


def _distribution(values: list[float]) -> dict[str, Any]:
    clean = [float(value) for value in values]
    if not clean:
        return {
            "sample_count": 0, "min": None, "average": None, "p50": None,
            "p90": None, "p95": None, "p99": None,
            "p95_observed": None, "p99_observed": None,
            "p95_reliable": False, "p99_reliable": False, "max": None,
        }
    p95_observed = _percentile(clean, 95)
    p99_observed = _percentile(clean, 99)
    sample_count = len(clean)
    return {
        "sample_count": sample_count,
        "min": _round_metric(min(clean)),
        "average": _round_metric(statistics.fmean(clean)),
        "p50": _round_metric(_percentile(clean, 50)),
        "p90": _round_metric(_percentile(clean, 90)),
        "p95": _round_metric(p95_observed) if sample_count >= MIN_RELIABLE_TAIL_SAMPLES else None,
        "p99": _round_metric(p99_observed) if sample_count >= MIN_RELIABLE_TAIL_SAMPLES else None,
        "p95_observed": _round_metric(p95_observed),
        "p99_observed": _round_metric(p99_observed),
        "p95_reliable": sample_count >= MIN_RELIABLE_TAIL_SAMPLES,
        "p99_reliable": sample_count >= MIN_RELIABLE_TAIL_SAMPLES,
        "max": _round_metric(max(clean)),
    }


class _MeasuredResponse:
    """Measure body arrival while preserving urllib's response interface."""

    def __init__(self, response: Any, sample: dict[str, Any], chunk_size: int = 8192):
        self._response = response
        self._sample = sample
        self._chunk_size = chunk_size

    def _record(self, data: bytes) -> None:
        now = time.perf_counter()
        if data:
            if self._sample["first_byte_perf"] is None:
                self._sample["first_byte_perf"] = now
            self._sample["last_byte_perf"] = now
            self._sample["response_bytes"] += len(data)
            if len(self._sample["response_body"]) < 2_000_000:
                self._sample["response_body"].extend(
                    data[: 2_000_000 - len(self._sample["response_body"])]
                )
        self._sample["finished_perf"] = now

    def read(self, size: int = -1) -> bytes:
        if size is not None and size >= 0:
            data = self._response.read(size)
            self._record(data)
            return data
        chunks = []
        first_chunk = self._response.read(1)
        self._record(first_chunk)
        if not first_chunk:
            return b""
        chunks.append(first_chunk)
        while True:
            chunk = self._response.read(self._chunk_size)
            self._record(chunk)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)

    def close(self) -> None:
        self._sample["finished_perf"] = self._sample["finished_perf"] or time.perf_counter()
        self._response.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, tb):
        self.close()
        return False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)


def _finish_network_sample(sample: dict[str, Any]) -> dict[str, Any]:
    start = sample.pop("started_perf")
    headers = sample.pop("headers_perf", None)
    first = sample.pop("first_byte_perf", None)
    last = sample.pop("last_byte_perf", None)
    finished = sample.pop("finished_perf", None) or time.perf_counter()
    sample["_started_perf"] = start
    sample["_finished_perf"] = finished
    sample["headers_elapsed_ms"] = _round_metric((headers - start) * 1000 if headers else None)
    sample["ttfb_ms"] = _round_metric((first - start) * 1000 if first else None)
    sample["_first_byte_perf"] = first
    sample["response_transfer_ms"] = _round_metric((last - first) * 1000 if first and last else None)
    sample["network_total_elapsed_ms"] = _round_metric((finished - start) * 1000)
    body = sample.pop("response_body", b"")
    if body:
        decoded_body = body.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(decoded_body)
            sample["business_error"] = _find_business_error(parsed)
            if sample["business_error"] or (isinstance(sample.get("status_code"), int) and sample["status_code"] >= 400):
                sample["response_error_preview"] = decoded_body[:1000]
                sample["response_error_body"] = decoded_body
                sample["response_error_body_truncated"] = sample["response_bytes"] > len(body)
            prompt_tokens, completion_tokens, total_tokens = _extract_token_usage(parsed)
            sample["prompt_tokens"] = prompt_tokens
            sample["completion_tokens"] = completion_tokens
            sample["total_tokens"] = total_tokens
        except (TypeError, ValueError, json.JSONDecodeError):
            sample["business_error"] = ""
            if isinstance(sample.get("status_code"), int) and sample["status_code"] >= 400:
                sample["response_error_preview"] = decoded_body[:1000]
                sample["response_error_body"] = decoded_body
                sample["response_error_body_truncated"] = sample["response_bytes"] > len(body)
            sample["prompt_tokens"] = None
            sample["completion_tokens"] = None
            sample["total_tokens"] = None
    else:
        sample["business_error"] = ""
        sample["prompt_tokens"] = None
        sample["completion_tokens"] = None
        sample["total_tokens"] = None
    return sample


@contextmanager
def _network_probe():
    """Patch urlopen once and route measurements to the calling worker thread."""
    original_urlopen = urllib.request.urlopen

    def measured_urlopen(request, *args, **kwargs):
        collector = getattr(_NETWORK_CONTEXT, "collector", None)
        if collector is None:
            return original_urlopen(request, *args, **kwargs)
        started = time.perf_counter()
        request_data = getattr(request, "data", None)
        sample = {
            "started_perf": started,
            "headers_perf": None,
            "first_byte_perf": None,
            "last_byte_perf": None,
            "finished_perf": None,
            "status_code": None,
            "request_bytes": len(request_data) if isinstance(request_data, bytes) else 0,
            "response_bytes": 0,
            "response_body": bytearray(),
            "error": None,
        }
        collector.append(sample)
        try:
            response = original_urlopen(request, *args, **kwargs)
            sample["headers_perf"] = time.perf_counter()
            sample["status_code"] = getattr(response, "status", None) or response.getcode()
            return _MeasuredResponse(response, sample)
        except urllib.error.HTTPError as exc:
            sample["headers_perf"] = time.perf_counter()
            sample["status_code"] = exc.code
            sample["error"] = f"HTTPError: {exc}"
            original_error_read = exc.read

            def measured_error_read(size: int = -1) -> bytes:
                if size is not None and size >= 0:
                    data = original_error_read(size)
                    now = time.perf_counter()
                    if data:
                        sample["first_byte_perf"] = sample["first_byte_perf"] or now
                        sample["last_byte_perf"] = now
                        sample["response_bytes"] += len(data)
                        if len(sample["response_body"]) < 2_000_000:
                            sample["response_body"].extend(data[: 2_000_000 - len(sample["response_body"])])
                    sample["finished_perf"] = now
                    return data
                chunks = []
                first_chunk = original_error_read(1)
                now = time.perf_counter()
                if first_chunk:
                    sample["first_byte_perf"] = now
                    sample["last_byte_perf"] = now
                    sample["response_bytes"] += 1
                    sample["response_body"].extend(first_chunk)
                    chunks.append(first_chunk)
                while first_chunk:
                    chunk = original_error_read(8192)
                    now = time.perf_counter()
                    if not chunk:
                        sample["finished_perf"] = now
                        break
                    sample["last_byte_perf"] = now
                    sample["response_bytes"] += len(chunk)
                    if len(sample["response_body"]) < 2_000_000:
                        sample["response_body"].extend(chunk[: 2_000_000 - len(sample["response_body"])])
                    chunks.append(chunk)
                sample["finished_perf"] = sample["finished_perf"] or now
                return b"".join(chunks)

            exc.read = measured_error_read
            raise
        except Exception as exc:
            sample["finished_perf"] = time.perf_counter()
            sample["error"] = f"{type(exc).__name__}: {exc}"
            raise

    urllib.request.urlopen = measured_urlopen
    try:
        yield
    finally:
        urllib.request.urlopen = original_urlopen


def _set_resume_id(value: Any, resume_id: str) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in ("resumeId", "resumeid", "resume_id"):
                value[key] = resume_id
            else:
                _set_resume_id(nested, resume_id)
    elif isinstance(value, list):
        for nested in value:
            _set_resume_id(nested, resume_id)


class _InflightTracker:
    def __init__(self) -> None:
        self.current = 0
        self.peak = 0
        self._lock = threading.Lock()

    def enter(self) -> None:
        with self._lock:
            self.current += 1
            self.peak = max(self.peak, self.current)

    def exit(self) -> None:
        with self._lock:
            self.current -= 1


class _CallbackCapture:
    """Thread-safe O(1) callback lookup for high-concurrency runs."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._items.clear()

    def put(self, kind: str, resume_id: Any, payload: Any) -> None:
        item = {
            "kind": kind,
            "resume_id": resume_id,
            "payload": copy.deepcopy(payload),
        }
        with self._lock:
            self._items[(kind, str(resume_id))] = item

    def take(self, kind: str, resume_id: Any) -> dict[str, Any] | None:
        with self._lock:
            return self._items.pop((kind, str(resume_id)), None)

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(list(self._items.values()))


def _install_callback_capture(callbacks: Any) -> None:
    """Replace the single-file callback adapter's global list with an indexed store."""
    capture = _CallbackCapture()

    def callback_profile_result(payload: Any, resume_id: Any, url: str) -> None:
        capture.put("profile", resume_id, payload)

    def callback_interview_result(payload: Any, url: str) -> None:
        resume_id = payload.get("resumeId") if isinstance(payload, dict) else None
        capture.put("report", resume_id, payload)

    def callback_question_answer(payload: Any, resume_id: Any, url: str) -> None:
        capture.put("questions", resume_id, payload)

    callbacks.reset_captured_results = capture.reset
    callbacks.get_captured_results = capture.all
    callbacks.pop_captured_result = capture.take
    callbacks.callback_profile_result = callback_profile_result
    callbacks.callback_interview_result = callback_interview_result
    callbacks.callback_question_answer = callback_question_answer


def _execute_business_request(
    analyze_service: Any,
    callbacks: Any,
    recruit: str,
    task: str,
    recruit_type: int,
    base_body: dict[str, Any],
    base_resume_id: Any,
    temperature: float,
    request_number: int,
    burst_id: int,
    start_barrier: threading.Barrier,
    inflight: _InflightTracker,
) -> dict[str, Any]:
    unique_resume_id = f"{base_resume_id}__load_{request_number}"
    body = json.loads(json.dumps(base_body, ensure_ascii=False))
    _set_resume_id(body, unique_resume_id)
    network_samples: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "request_number": request_number,
        "burst_id": burst_id,
        "resume_id": unique_resume_id,
        "success": False,
        "error_category": None,
        "error": None,
        "started_epoch": None,
        "ended_epoch": None,
        "business_wall_elapsed_ms": None,
        "send_perf": 0.0,
        "business_finished_perf": None,
        "inflight_started_perf": None,
        "inflight_finished_perf": None,
        "total_ms": 0.0,
        "status_code": None,
        "first_byte_ms": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "output_chars": 0,
        "sent": False,
        "network_calls": network_samples,
    }
    try:
        start_barrier.wait()
    except threading.BrokenBarrierError as exc:
        result["error_category"] = "start_barrier_error"
        result["error"] = str(exc)
        return result

    started_perf = time.perf_counter()
    result["send_perf"] = started_perf
    result["sent"] = True
    result["started_epoch"] = time.time()
    inflight.enter()
    result["inflight_started_perf"] = time.perf_counter()
    _NETWORK_CONTEXT.collector = network_samples

    measurement_finished = False

    def finish_business_measurement() -> None:
        nonlocal measurement_finished
        if measurement_finished:
            return
        ended_perf = time.perf_counter()
        result["business_finished_perf"] = ended_perf
        result["inflight_finished_perf"] = ended_perf
        result["ended_epoch"] = time.time()
        result["business_wall_elapsed_ms"] = _round_metric((ended_perf - started_perf) * 1000)
        for sample in network_samples:
            if sample.get("finished_perf") is None:
                sample["finished_perf"] = ended_perf
        _NETWORK_CONTEXT.collector = None
        inflight.exit()
        measurement_finished = True

    try:
        try:
            if task == "profile":
                analyze_service.async_analyze_profile(unique_resume_id, body, recruit_type, temperature)
            elif task == "questions":
                analyze_service.async_generate_question_answers(unique_resume_id, body, recruit_type, temperature)
            else:
                analyze_service.async_generate_report(unique_resume_id, body, recruit_type, temperature)
        finally:
            # Payload validation and report hashing are measurement bookkeeping,
            # not part of the production business-call latency.
            finish_business_measurement()
        captured = callbacks.pop_captured_result(task, unique_resume_id)
        if captured is None:
            result["error_category"] = "Business error"
            raise RuntimeError(f"业务执行结束但没有捕获到唯一回调：{unique_resume_id}")
        payload = captured.get("payload")
        callback_bytes = json.dumps(
            payload, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        result["callback_payload_bytes"] = len(callback_bytes)
        result["output_chars"] = len(callback_bytes.decode("utf-8", errors="replace").strip())
        if payload is None or payload == "" or payload == {} or payload == [] or not callback_bytes.decode("utf-8", errors="replace").strip():
            result["error_category"] = "Empty output"
            raise RuntimeError("Empty output")
        business_error = _find_business_error(payload)
        if business_error:
            result["error_category"] = "Business error"
            raise RuntimeError(business_error)
        prompt_tokens, completion_tokens, total_tokens = _extract_token_usage(payload)
        result["prompt_tokens"] = prompt_tokens
        result["completion_tokens"] = completion_tokens
        result["total_tokens"] = total_tokens
        result["success"] = True
        result["callback_payload_sha256"] = hashlib.sha256(callback_bytes).hexdigest()
    except Exception as exc:
        result["error_category"] = result.get("error_category") or type(exc).__name__
        result["error"] = str(exc)
    finally:
        finish_business_measurement()
        result["network_calls"] = [_finish_network_sample(sample) for sample in network_samples]
        statuses = [call.get("status_code") for call in result["network_calls"] if call.get("status_code") is not None]
        result["status_code"] = statuses[-1] if statuses else None
        valid_calls = [
            call for call in result["network_calls"]
            if isinstance(call.get("status_code"), int)
            and 200 <= call["status_code"] < 300
            and call.get("error") is None
        ]
        successful_first_byte_points = [
            call.get("_first_byte_perf") for call in valid_calls
            if call.get("_first_byte_perf") is not None
        ]
        result["first_byte_ms"] = _round_metric(
            (min(successful_first_byte_points) - started_perf) * 1000
        ) if successful_first_byte_points else None
        for call in result["network_calls"]:
            call.pop("_first_byte_perf", None)
        if result["success"]:
            for token_key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                call_tokens = [call.get(token_key) for call in valid_calls]
                # A business operation can invoke the gateway multiple times.
                # Only the sum of complete per-call usage is an auditable total.
                result[token_key] = (
                    sum(call_tokens)
                    if valid_calls and all(value is not None for value in call_tokens)
                    else None
                )
            if not valid_calls:
                result["success"] = False
                result["error_category"] = "HTTP error" if statuses else "Connection error"
                result["error"] = f"HTTP status {result['status_code']}" if statuses else "No gateway response"
            elif any(call.get("business_error") for call in valid_calls):
                result["success"] = False
                result["error_category"] = "Business error"
                result["error"] = next(call["business_error"] for call in valid_calls if call.get("business_error"))
            elif not any(call.get("response_bytes", 0) > 0 for call in valid_calls):
                result["success"] = False
                result["error_category"] = "Empty output"
                result["error"] = "Empty gateway response"
        network_total = sum(sample["network_total_elapsed_ms"] for sample in result["network_calls"])
        transfer_values = [
            sample["response_transfer_ms"] for sample in result["network_calls"]
            if sample["response_transfer_ms"] is not None
        ]
        result["network_total_elapsed_ms"] = (
            _round_metric(network_total) if result["network_calls"] else None
        )
        result["response_transfer_ms"] = _round_metric(sum(transfer_values)) if transfer_values else None
        result["request_bytes"] = sum(sample["request_bytes"] for sample in result["network_calls"])
        result["response_bytes"] = sum(sample["response_bytes"] for sample in result["network_calls"])
        ended_perf = time.perf_counter()
        result["total_ms"] = _round_metric((ended_perf - started_perf) * 1000)
    return result


def _summarize_step(
    concurrency: int,
    rounds: int,
    wall_clock_seconds: float,
    observed_peak: int,
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    attempted = len(results)
    succeeded = [item for item in results if item["success"]]
    failed = attempted - len(succeeded)
    sent_results = [item for item in results if item.get("sent") and item.get("send_perf", 0) > 0]
    burst_durations = []
    burst_measurements = []
    full_bursts = 0
    for burst_id in sorted({item.get("burst_id") for item in sent_results}):
        burst_results = [item for item in sent_results if item.get("burst_id") == burst_id]
        if not burst_results:
            continue
        burst_start = min(item["send_perf"] for item in burst_results)
        burst_end = max(item["business_finished_perf"] for item in burst_results)
        burst_duration = max(burst_end - burst_start, 0.0)
        burst_durations.append(burst_duration)
        events = [
            event
            for item in burst_results
            for event in (
                (item["inflight_started_perf"], 1),
                (item["inflight_finished_perf"], -1),
            )
        ]
        active = 0
        burst_peak = 0
        for _timestamp, delta in sorted(events, key=lambda event: (event[0], event[1])):
            active += delta
            burst_peak = max(burst_peak, active)
        burst_measurements.append({
            "burst_id": burst_id,
            "sent_requests": len(burst_results),
            "success_count": sum(1 for item in burst_results if item.get("success")),
            "failed_count": sum(1 for item in burst_results if not item.get("success")),
            "duration_seconds": _round_metric(burst_duration),
            "observed_peak_inflight": burst_peak,
        })
        if len(burst_results) == concurrency and burst_peak == concurrency:
            full_bursts += 1
    active_burst_duration = sum(burst_durations)
    measurement_duration = max(0.0, (
        max(item["business_finished_perf"] for item in sent_results)
        - min(item["send_perf"] for item in sent_results)
        if sent_results else 0.0
    ))
    all_network_calls = [call for item in results for call in item["network_calls"]]
    successful_network_calls = [
        call for call in all_network_calls
        if isinstance(call.get("status_code"), int)
        and 200 <= call["status_code"] < 300
        and call.get("error") is None
    ]
    gateway_events = [
        event
        for call in all_network_calls
        if call.get("_started_perf") is not None and call.get("_finished_perf") is not None
        for event in (
            (call["_started_perf"], 1),
            (call["_finished_perf"], -1),
        )
    ]
    gateway_active = 0
    observed_peak_gateway_calls = 0
    for _timestamp, delta in sorted(gateway_events, key=lambda event: (event[0], event[1])):
        gateway_active += delta
        observed_peak_gateway_calls = max(observed_peak_gateway_calls, gateway_active)
    for call in all_network_calls:
        call.pop("_started_perf", None)
        call.pop("_finished_perf", None)
    response_transfer = [item["response_transfer_ms"] for item in succeeded if item["response_transfer_ms"] is not None]
    all_response_transfer = [
        item["response_transfer_ms"] for item in sent_results
        if item.get("response_transfer_ms") is not None
    ]
    status_counts = Counter(str(call["status_code"]) for call in all_network_calls if call["status_code"] is not None)
    error_counts = Counter(
        _normalize_error(item.get("error"), item.get("error_category"))
        for item in results if not item["success"]
    )
    successful_gateway_calls = len(successful_network_calls)
    failed_gateway_calls = len(all_network_calls) - successful_gateway_calls
    wall = max(0.0, wall_clock_seconds)
    success_rate = len(succeeded) / attempted * 100 if attempted else 0.0
    response_times = [item["business_wall_elapsed_ms"] for item in succeeded if item.get("business_wall_elapsed_ms") is not None]
    all_request_times = [item["business_wall_elapsed_ms"] for item in sent_results if item.get("business_wall_elapsed_ms") is not None]
    ttfb_times = [item["first_byte_ms"] for item in succeeded if item.get("first_byte_ms") is not None]
    prompt_tokens = [item["prompt_tokens"] for item in succeeded if item.get("prompt_tokens") is not None]
    completion_tokens = [item["completion_tokens"] for item in succeeded if item.get("completion_tokens") is not None]
    total_tokens = [item["total_tokens"] for item in succeeded if item.get("total_tokens") is not None]
    total_prompt_tokens = sum(prompt_tokens)
    total_completion_tokens = sum(completion_tokens)
    total_token_count = sum(total_tokens)
    prompt_coverage = len(prompt_tokens) / len(succeeded) * 100 if succeeded else 0.0
    completion_coverage = len(completion_tokens) / len(succeeded) * 100 if succeeded else 0.0
    total_coverage = len(total_tokens) / len(succeeded) * 100 if succeeded else 0.0
    prompt_usage_complete = bool(succeeded) and len(prompt_tokens) == len(succeeded)
    completion_usage_complete = bool(succeeded) and len(completion_tokens) == len(succeeded)
    total_usage_complete = bool(succeeded) and len(total_tokens) == len(succeeded)
    success_network_total = [item["network_total_elapsed_ms"] for item in succeeded if item.get("network_total_elapsed_ms") is not None]
    all_network_total = [
        item["network_total_elapsed_ms"] for item in sent_results
        if item.get("network_total_elapsed_ms") is not None
    ]
    all_gateway_prompt_tokens = [call.get("prompt_tokens") for call in successful_network_calls]
    all_gateway_completion_tokens = [call.get("completion_tokens") for call in successful_network_calls]
    all_gateway_total_tokens = [call.get("total_tokens") for call in successful_network_calls]
    gateway_prompt_complete = bool(successful_network_calls) and all(
        value is not None for value in all_gateway_prompt_tokens
    )
    gateway_completion_complete = bool(successful_network_calls) and all(
        value is not None for value in all_gateway_completion_tokens
    )
    gateway_total_complete = bool(successful_network_calls) and all(
        value is not None for value in all_gateway_total_tokens
    )
    gateway_prompt_total = sum(value for value in all_gateway_prompt_tokens if value is not None)
    gateway_completion_total = sum(value for value in all_gateway_completion_tokens if value is not None)
    gateway_total = sum(value for value in all_gateway_total_tokens if value is not None)
    quality_warnings = []
    failed_request_records = _failed_request_records(results, concurrency)
    gateway_failure_records = _gateway_failure_records(results, concurrency)
    if len(response_times) < MIN_RELIABLE_TAIL_SAMPLES:
        quality_warnings.append(
            f"成功延迟样本仅 {len(response_times)} 个，P95/P99 已标记 N/A；"
            f"可靠尾部分位至少需要 {MIN_RELIABLE_TAIL_SAMPLES} 个样本，原始观测值保留在 *_observed"
        )
    if full_bursts < rounds:
        quality_warnings.append(
            f"仅 {full_bursts}/{rounds} 个批次实际达到目标业务并发"
        )
    if succeeded and not (prompt_usage_complete and completion_usage_complete and total_usage_complete):
        quality_warnings.append("Token usage 未覆盖全部成功业务请求，缺失口径的 token 吞吐已输出 N/A")
    if successful_network_calls and not (
        gateway_prompt_complete and gateway_completion_complete and gateway_total_complete
    ):
        quality_warnings.append("部分成功网关调用缺少 usage；全量网关 token 吞吐已输出 N/A")
    if failed:
        quality_warnings.append(
            f"成功业务延迟/网络平均仅覆盖 {len(succeeded)}/{attempted} 个业务请求；"
            "全量请求分布才包含失败和超时请求"
        )
    if gateway_failure_records:
        quality_warnings.append(
            f"发现 {len(gateway_failure_records)} 次网关失败尝试；业务成功可能由重试恢复，"
            "成功业务网络平均不代表单次网关调用平均"
        )
    return {
        "concurrency": concurrency,
        "rounds": rounds,
        "attempted_requests": attempted,
        "sent_requests": len(sent_results),
        "completed_requests": len(results),
        "success_count": len(succeeded),
        "failed_count": failed,
        "success_rate_percent": _round_metric(success_rate),
        "error_rate_percent": _round_metric(100 - success_rate),
        "wall_clock_seconds": _round_metric(wall),
        "measurement_duration_seconds": _round_metric(measurement_duration),
        "effective_duration_seconds": _round_metric(measurement_duration),
        "active_burst_duration_seconds": _round_metric(active_burst_duration),
        "tail_percentile_min_samples": MIN_RELIABLE_TAIL_SAMPLES,
        "burst_rounds": rounds,
        "full_concurrency_bursts": full_bursts,
        "burst_measurements": burst_measurements,
        "metric_quality_warnings": quality_warnings,
        "failed_request_records": failed_request_records,
        "gateway_failure_records": gateway_failure_records,
        "throughput": {
            "sent_requests_per_second": _round_metric(len(sent_results) / measurement_duration if measurement_duration else 0.0),
            "attempted_requests_per_second": _round_metric(len(sent_results) / measurement_duration if measurement_duration else 0.0),
            "successful_requests_per_second": _round_metric(len(succeeded) / measurement_duration if measurement_duration else 0.0),
            "total_qps": _round_metric(len(sent_results) / measurement_duration if measurement_duration else 0.0),
            "success_qps": _round_metric(len(succeeded) / measurement_duration if measurement_duration else 0.0),
            "active_burst_total_qps": _round_metric(len(sent_results) / active_burst_duration if active_burst_duration else 0.0),
            "active_burst_success_qps": _round_metric(len(succeeded) / active_burst_duration if active_burst_duration else 0.0),
            "gateway_attempt_qps": _round_metric(len(all_network_calls) / measurement_duration if measurement_duration else 0.0),
            "gateway_success_qps": _round_metric(successful_gateway_calls / measurement_duration if measurement_duration else 0.0),
            "input_token_throughput": _round_metric(total_prompt_tokens / measurement_duration if prompt_usage_complete and measurement_duration else None),
            "output_token_throughput": _round_metric(total_completion_tokens / measurement_duration if completion_usage_complete and measurement_duration else None),
            "total_token_throughput": _round_metric(total_token_count / measurement_duration if total_usage_complete and measurement_duration else None),
            "response_megabytes_per_second": _round_metric(
                sum(item["response_bytes"] for item in succeeded) / 1_000_000 / measurement_duration
                if measurement_duration else 0.0
            ),
            "all_response_megabytes_per_second": _round_metric(
                sum(item["response_bytes"] for item in sent_results) / 1_000_000 / measurement_duration
                if measurement_duration else 0.0
            ),
        },
        "business_wall_elapsed_ms": {
            "successful_requests": _distribution(response_times),
            "all_requests": _distribution(all_request_times),
        },
        "complete_network_transfer_total_ms": _distribution(success_network_total),
        "all_request_network_transfer_total_ms": _distribution(all_network_total),
        "response_completion_first_to_last_byte_ms": _distribution(response_transfer),
        "all_request_response_completion_first_to_last_byte_ms": _distribution(all_response_transfer),
        "gateway_call_metrics_ms": {
            "network_total_elapsed": _distribution([
                call["network_total_elapsed_ms"] for call in successful_network_calls
                if call.get("network_total_elapsed_ms") is not None
            ]),
            "headers_elapsed": _distribution([
                call["headers_elapsed_ms"] for call in successful_network_calls
                if call["headers_elapsed_ms"] is not None
            ]),
            "time_to_first_byte": _distribution([
                call["ttfb_ms"] for call in successful_network_calls if call["ttfb_ms"] is not None
            ]),
            "first_to_last_byte": _distribution([
                call["response_transfer_ms"] for call in successful_network_calls
                if call["response_transfer_ms"] is not None
            ]),
        },
        "all_gateway_call_metrics_ms": {
            "network_total_elapsed": _distribution([
                call["network_total_elapsed_ms"] for call in all_network_calls
                if call.get("network_total_elapsed_ms") is not None
            ]),
            "headers_elapsed": _distribution([
                call["headers_elapsed_ms"] for call in all_network_calls
                if call.get("headers_elapsed_ms") is not None
            ]),
            "time_to_first_byte": _distribution([
                call["ttfb_ms"] for call in all_network_calls if call.get("ttfb_ms") is not None
            ]),
            "first_to_last_byte": _distribution([
                call["response_transfer_ms"] for call in all_network_calls
                if call.get("response_transfer_ms") is not None
            ]),
        },
        "response_latency_ms": _distribution(response_times),
        "all_request_end_to_end_ms": _distribution(all_request_times),
        "ttfb_ms": _distribution(ttfb_times),
        "ttft_ms": _distribution([]),
        "token_usage": {
            "total_prompt_tokens": total_prompt_tokens,
            "total_completion_tokens": total_completion_tokens,
            "total_tokens": total_token_count,
            "prompt_usage_coverage_percent": _round_metric(prompt_coverage),
            "completion_usage_coverage_percent": _round_metric(completion_coverage),
            "total_usage_coverage_percent": _round_metric(total_coverage),
            "coverage_percent": _round_metric(completion_coverage),
            "complete": completion_usage_complete,
            "input_token_throughput": _round_metric(total_prompt_tokens / measurement_duration if prompt_usage_complete and measurement_duration else None),
            "output_token_throughput": _round_metric(total_completion_tokens / measurement_duration if completion_usage_complete and measurement_duration else None),
            "total_token_throughput": _round_metric(total_token_count / measurement_duration if total_usage_complete and measurement_duration else None),
            "average_output_tokens_per_successful_request": _round_metric(
                total_completion_tokens / len(succeeded) if completion_usage_complete else None
            ),
            "gateway_total_prompt_tokens": gateway_prompt_total,
            "gateway_total_completion_tokens": gateway_completion_total,
            "gateway_total_tokens": gateway_total,
            "gateway_prompt_usage_coverage_percent": _round_metric(
                sum(value is not None for value in all_gateway_prompt_tokens)
                / len(successful_network_calls) * 100 if successful_network_calls else 0.0
            ),
            "gateway_completion_usage_coverage_percent": _round_metric(
                sum(value is not None for value in all_gateway_completion_tokens)
                / len(successful_network_calls) * 100 if successful_network_calls else 0.0
            ),
            "gateway_total_usage_coverage_percent": _round_metric(
                sum(value is not None for value in all_gateway_total_tokens)
                / len(successful_network_calls) * 100 if successful_network_calls else 0.0
            ),
            "gateway_input_token_throughput": _round_metric(
                gateway_prompt_total / measurement_duration
                if gateway_prompt_complete and measurement_duration else None
            ),
            "gateway_output_token_throughput": _round_metric(
                gateway_completion_total / measurement_duration
                if gateway_completion_complete and measurement_duration else None
            ),
            "gateway_total_token_throughput": _round_metric(
                gateway_total / measurement_duration
                if gateway_total_complete and measurement_duration else None
            ),
        },
        "traffic": {
            "gateway_call_count": len(all_network_calls),
            "successful_business_request_bytes": sum(item["request_bytes"] for item in succeeded),
            "successful_business_response_bytes": sum(item["response_bytes"] for item in succeeded),
        },
        "concurrency_observation": {
            "configured": concurrency,
            "observed_peak_business_requests": observed_peak,
            "observed_peak_gateway_calls": observed_peak_gateway_calls,
            "target_concurrency": concurrency,
            "observed_peak_inflight": observed_peak,
        },
        "errors": {
            "business_error_rate_percent": _round_metric(100 - success_rate),
            "gateway_attempt_count": len(all_network_calls),
            "gateway_success_count": successful_gateway_calls,
            "gateway_failure_count": failed_gateway_calls,
            "gateway_error_rate_percent": _round_metric(
                failed_gateway_calls / len(all_network_calls) * 100 if all_network_calls else 0.0
            ),
            "business_error_category_counts": dict(sorted(error_counts.items())),
            "error_summary": dict(sorted(error_counts.items())),
            "gateway_http_status_counts": dict(sorted(status_counts.items())),
        },
    }


def _print_step(step: dict[str, Any], success_threshold: float) -> None:
    latency = step["business_wall_elapsed_ms"]["successful_requests"]
    network = step["complete_network_transfer_total_ms"]
    all_network = step["all_request_network_transfer_total_ms"]
    transfer = step["response_completion_first_to_last_byte_ms"]
    all_transfer = step["all_request_response_completion_first_to_last_byte_ms"]
    throughput = step["throughput"]
    token_usage = step["token_usage"]

    def token_metric(value: float | None, suffix: str = " tok/s") -> str:
        return "N/A" if value is None else f"{value:.6f}{suffix}"

    def tail_metric(distribution: dict[str, Any], key: str) -> str:
        formal = distribution.get(key)
        if formal is not None:
            return _format_ms(formal)
        observed = distribution.get(f"{key}_observed")
        return "N/A" if observed is None else f"N/A(观测={_format_ms(observed)})"
    print(f"\n并发数: {step['concurrency']}（非流式）")
    print(
        f"请求统计: 计划={step['attempted_requests']}，已发送={step['sent_requests']}，"
        f"完成={step['completed_requests']}，成功={step['success_count']}，"
        f"失败={step['failed_count']}，成功率={step['success_rate_percent']:.6f}%"
    )
    round_results = "；".join(
        f"第{burst['burst_id']}轮 成功={burst['success_count']}/失败={burst['failed_count']}"
        for burst in step.get("burst_measurements", [])
    )
    if round_results:
        print(f"分轮结果: {round_results}")
    failed_records = step.get("failed_request_records", [])
    for record in failed_records[:5]:
        print(
            f"失败请求: request={record.get('request_number')}，"
            f"burst={record.get('burst_id')}，status={record.get('status_code') or 'N/A'}，"
            f"后端错误={record['error_summary']}"
        )
    if len(failed_records) > 5:
        print(f"失败请求错误摘要仅显示前5条，剩余 {len(failed_records) - 5} 条已写入错误文件")
    gateway_errors = step["errors"]
    print(
        f"网关调用统计: 总计={gateway_errors['gateway_attempt_count']}，"
        f"成功={gateway_errors['gateway_success_count']}，"
        f"失败={gateway_errors['gateway_failure_count']}，"
        f"错误率={gateway_errors['gateway_error_rate_percent']:.6f}%"
    )
    gateway_failure_records = step.get("gateway_failure_records", [])
    for record in gateway_failure_records[:5]:
        recovered = "，最终业务已成功（重试恢复）" if record["business_request_success"] else ""
        print(
            f"网关失败: request={record.get('request_number')}，"
            f"call={record.get('gateway_call_index')}，status={record.get('status_code') or 'N/A'}，"
            f"后端错误={record['error_summary']}{recovered}"
        )
    if len(gateway_failure_records) > 5:
        print(f"网关错误摘要仅显示前5条，剩余 {len(gateway_failure_records) - 5} 条已写入错误文件")
    print(
        f"墙钟时间: {step['wall_clock_seconds']:.6f}s，"
        f"测量窗口: {step['measurement_duration_seconds']:.6f}s，"
        f"活跃批次耗时和: {step['active_burst_duration_seconds']:.6f}s"
    )
    print(
        "成功业务完整网络传输总耗时: "
        f"平均={_format_ms(network['average'])}, "
        f"P95={tail_metric(network, 'p95')}, P99={tail_metric(network, 'p99')}"
    )
    print(
        "全量已发送请求完整网络传输总耗时: "
        f"平均={_format_ms(all_network['average'])}, "
        f"P95={tail_metric(all_network, 'p95')}, P99={tail_metric(all_network, 'p99')}，"
        f"样本数={all_network['sample_count']}"
    )
    print(
        "成功业务响应完成时间(首字节-最后字节): "
        f"平均={_format_ms(transfer['average'])}, "
        f"P95={tail_metric(transfer, 'p95')}, P99={tail_metric(transfer, 'p99')}"
    )
    print(
        "全量已发送请求响应完成时间(首字节-最后字节): "
        f"平均={_format_ms(all_transfer['average'])}, "
        f"P95={tail_metric(all_transfer, 'p95')}, P99={tail_metric(all_transfer, 'p99')}，"
        f"样本数={all_transfer['sample_count']}"
    )
    print(
        f"实际吞吐量: 总QPS={throughput['total_qps']:.6f}，"
        f"成功QPS={throughput['success_qps']:.6f}（首个请求发出至最后请求完成）"
    )
    print(
        f"活跃批次吞吐量: 总QPS={throughput['active_burst_total_qps']:.6f}，"
        f"成功QPS={throughput['active_burst_success_qps']:.6f}（仅用于观察，剔除批次间隙）"
    )
    print(
        f"网关吞吐量: 尝试QPS={throughput['gateway_attempt_qps']:.6f}，"
        f"成功QPS={throughput['gateway_success_qps']:.6f}"
    )
    print(
        "成功业务 P95/P99 响应时间: "
        f"P95={tail_metric(latency, 'p95')}, P99={tail_metric(latency, 'p99')}，"
        f"样本数={latency['sample_count']}"
    )
    print(f"错误率: {step['error_rate_percent']:.6f}%")
    print(
        "端到端耗时(全部已发送): "
        f"平均={_format_ms(step['all_request_end_to_end_ms']['average'])}, "
        f"P95={tail_metric(step['all_request_end_to_end_ms'], 'p95')}, "
        f"P99={tail_metric(step['all_request_end_to_end_ms'], 'p99')}"
    )
    print("首 token 延迟(TTFT): N/A（固定非流式模式）")
    print(
        "成功业务Token吞吐: "
        f"输入={token_metric(token_usage['input_token_throughput'])}，"
        f"输出={token_metric(token_usage['output_token_throughput'])}，"
        f"总计={token_metric(token_usage['total_token_throughput'])}"
    )
    print(
        "全量成功网关调用Token吞吐: "
        f"输入={token_metric(token_usage['gateway_input_token_throughput'])}，"
        f"输出={token_metric(token_usage['gateway_output_token_throughput'])}，"
        f"总计={token_metric(token_usage['gateway_total_token_throughput'])}"
    )
    print(
        "Token usage覆盖率: "
        f"输入={token_usage['prompt_usage_coverage_percent']:.2f}%/"
        f"输出={token_usage['completion_usage_coverage_percent']:.2f}%/"
        f"总计={token_usage['total_usage_coverage_percent']:.2f}%"
    )
    print(
        "平均每成功请求输出token数: "
        f"{token_metric(token_usage['average_output_tokens_per_successful_request'], ' tok/request')}"
    )
    print(
        f"满并发批次: {step['full_concurrency_bursts']}/{step['burst_rounds']}，"
        f"目标业务并发/业务峰值/网关峰值: "
        f"{step['concurrency_observation']['configured']}/"
        f"{step['concurrency_observation']['observed_peak_business_requests']}/"
        f"{step['concurrency_observation']['observed_peak_gateway_calls']}"
    )
    for warning in step["metric_quality_warnings"]:
        print(f"指标质量提示: {warning}")
    if step["success_rate_percent"] < success_threshold:
        print(f"拐点: 成功率 {step['success_rate_percent']:.6f}% < {success_threshold:.6f}%")


def _run_step(
    analyze_service: Any,
    callbacks: Any,
    recruit: str,
    task: str,
    recruit_type: int,
    body: dict[str, Any],
    resume_id: Any,
    temperature: float,
    concurrency: int,
    rounds: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    inflight = _InflightTracker()
    results: list[dict[str, Any]] = []
    request_number = 0
    wall_started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        for burst_id in range(1, rounds + 1):
            barrier = threading.Barrier(concurrency + 1)
            futures = []
            for _worker in range(concurrency):
                request_number += 1
                futures.append(executor.submit(
                    _execute_business_request, analyze_service, callbacks, recruit, task,
                    recruit_type, body, resume_id, temperature, request_number, burst_id, barrier, inflight,
                ))
            barrier.wait()
            for future in concurrent.futures.as_completed(futures):
                results.append(future.result())
    wall_elapsed = time.perf_counter() - wall_started
    results.sort(key=lambda item: item["request_number"])
    return _summarize_step(concurrency, rounds, wall_elapsed, inflight.peak, results), results


def _run_staircase(
    config: dict[str, Any],
    recruit: str,
    task: str,
    fixture_path: str | None,
    temperature: float,
    levels: list[int],
    rounds: int,
    success_threshold: float,
    out_dir: Path,
    skip_ping: bool,
) -> dict[str, Any]:
    model_dir = out_dir / _safe_name(config["model"])
    request_error_path = model_dir / f"{recruit}_{task}_concurrency_errors.json"
    _, qwen_client, callbacks = _load_business_runtime(config)
    with _suppress_business_logs():
        ping_info = None if skip_ping else _ping(qwen_client, config["timeout"])
        data = _load_fixture(fixture_path, recruit, task)
        resume_id, body, recruit_type = _adapt_request(data, recruit, task)
        analyze_service: Any = importlib.import_module("inference.analyze_service")
    callbacks.reset_captured_results()
    steps = []
    details = []
    breaking_point = None
    last_healthy = None

    print(f"\n测试项: {recruit}/{task}", flush=True)
    with _suppress_business_logs():
        for concurrency in levels:
            callbacks.reset_captured_results()
            step, request_results = _run_step(
                analyze_service, callbacks, recruit, task, recruit_type, body, resume_id,
                temperature, concurrency, rounds,
            )
            steps.append(step)
            details.extend({"concurrency": concurrency, **item} for item in request_results)
            accumulated_business_errors = [
                record for completed_step in steps
                for record in completed_step.get("failed_request_records", [])
            ]
            accumulated_gateway_errors = [
                record for completed_step in steps
                for record in completed_step.get("gateway_failure_records", [])
            ]
            _write_concurrency_errors(
                request_error_path, recruit, task,
                accumulated_business_errors, accumulated_gateway_errors,
            )
            _print_step(step, success_threshold)
            if step["success_rate_percent"] < success_threshold:
                breaking_point = {
                    "concurrency": concurrency,
                    "success_rate_percent": step["success_rate_percent"],
                    "reason": f"成功率 {step['success_rate_percent']:.6f}% < {success_threshold:.6f}%",
                }
                break
            last_healthy = concurrency

    healthy_steps = [step for step in steps if step["success_rate_percent"] >= success_threshold]
    peak_throughput = max(
        healthy_steps,
        key=lambda step: step["throughput"]["successful_requests_per_second"],
        default=None,
    )
    best = {
        "concurrency": last_healthy,
        "basis": "拐点前最后一个成功率达标的阶梯" if breaking_point else "当前已测试范围内最高的达标阶梯",
        "is_bounded_by_observed_breaking_point": breaking_point is not None and last_healthy is not None,
    }
    report = {
        "schema_version": 2,
        "generated_at": datetime.now().astimezone().isoformat(),
        "test_case": {"recruit": recruit, "task": task},
        "gateway": _public_config(config),
        "production_revision": PRODUCTION_REVISION,
        "settings": {
            "concurrency_levels": levels,
            "rounds_per_level": rounds,
            "stream": False,
            "mode": "staircase_non_stream",
            "success_threshold_percent": success_threshold,
            "percentile_method": "linear interpolation: sorted rank=(n-1)*p/100",
            "tail_percentile_min_samples": MIN_RELIABLE_TAIL_SAMPLES,
            "time_source": "time.perf_counter (monotonic)",
            "response_read_chunk_bytes": 8192,
        },
        "metric_definitions": {
            "wall_clock_seconds": "本阶梯从线程池启动到全部轮次完成的单调时钟差",
            "measurement_duration_seconds": "本阶梯首个请求实际发出到最后一个已发出请求完成的单调时钟差，包含批次切换间隙",
            "effective_duration_seconds": "measurement_duration_seconds 的兼容别名",
            "active_burst_duration_seconds": "各同步 burst 从首个请求发出到最后请求完成的时长之和，不含 burst 间隙，仅作为诊断口径",
            "business_wall_elapsed_ms": "单个生产业务函数从同步放行到返回或抛错的耗时；不含压测器后续 payload 校验、序列化和哈希",
            "complete_network_transfer_total_ms": "成功业务请求内所有网关 HTTP 尝试从 urlopen 前到响应体读完/异常的耗时之和；分布不含失败业务请求",
            "all_request_network_transfer_total_ms": "所有已发送业务请求内所有网关 HTTP 尝试从 urlopen 前到响应体读完/异常的耗时之和；包含业务失败和超时请求",
            "response_completion_first_to_last_byte_ms": "成功业务请求内所有有响应体网关调用的首个已读字节到最后已读字节耗时之和；分布不含失败业务请求",
            "all_request_response_completion_first_to_last_byte_ms": "所有已发送业务请求内有响应体网关调用的首个已读字节到最后已读字节耗时之和",
            "throughput": "业务总/成功请求数除以完整测量窗口；成功吞吐仅计完整业务成功请求。active_burst_* 指标另以活跃批次耗时和为分母",
            "gateway_throughput": "全部/HTTP成功网关调用数除以同一完整测量窗口；一次业务请求可以产生多次网关调用",
            "token_throughput": "成功业务请求内全部成功网关调用的输入、输出及总 token 数分别除以完整测量窗口；任一对应网关调用缺少 usage 时为 null",
            "gateway_token_throughput": "所有 HTTP 成功网关调用的 token 数除以完整测量窗口；包含最终业务失败但网关已成功的调用，usage 不完整时为 null",
            "average_output_tokens_per_successful_request": "所有成功业务请求的 completion token 总数除以成功业务请求数；completion usage 未完整覆盖时为 null",
            "success": "业务流程最终至少有一次 HTTP 2xx 且存在非空响应体、成功网关响应无业务错误、回调非空且 kind 与唯一 resume_id 匹配；允许流程内部发生后续重试成功的失败尝试",
            "gateway_error_rate_percent": "非 2xx、连接异常等失败网关尝试数除以全部网关尝试数；重试逐次计数",
            "token_usage_coverage": "按成功业务请求统计；只有该请求内每个成功网关调用都提供对应 usage，才计为已覆盖",
            "percentile_sample_count": "每个分布的 sample_count 是实际参与计算的样本数；P95/P99 样本不足时正式值返回 null，不推算",
            "percentile_reliability": f"P95/P99 仅在样本数 >= {MIN_RELIABLE_TAIL_SAMPLES} 时输出；不足时原始线性插值保留在 p95_observed/p99_observed",
            "full_concurrency_bursts": "实际同时在途业务请求峰值达到目标并发的批次数，不仅检查已提交请求数",
            "observed_peak_gateway_calls": "根据每次 urlopen 开始至响应读完或异常的区间计算的实际同时在途网关调用峰值",
            "business_ttfb_ms": "从业务请求同步放行到首个成功 HTTP 响应首字节；失败重试的响应首字节不混入",
            "ttft_ms": "非流式模式不产生流式文本增量，因此 TTFT 为 N/A；无流事件校验不适用",
        },
        "ping": ping_info,
        "steps": steps,
        "breaking_point": breaking_point,
        "best_concurrency_upper_bound": best,
        "peak_success_throughput_within_healthy_steps": None if peak_throughput is None else {
            "concurrency": peak_throughput["concurrency"],
            "successful_requests_per_second": peak_throughput["throughput"]["successful_requests_per_second"],
        },
        "request_details": details,
    }
    report_path = model_dir / f"{recruit}_{task}_concurrency_report.json"
    csv_path = model_dir / f"{recruit}_{task}_concurrency_summary.csv"
    failed_records = [
        record
        for step in steps
        for record in step.get("failed_request_records", [])
    ]
    gateway_failure_records = [
        record
        for step in steps
        for record in step.get("gateway_failure_records", [])
    ]
    _write_concurrency_errors(
        request_error_path, recruit, task, failed_records, gateway_failure_records,
    )
    report["request_error_log_path"] = str(request_error_path)
    report["failed_business_request_count"] = len(failed_records)
    report["failed_gateway_call_count"] = len(gateway_failure_records)
    _write_json(report_path, report)
    _write_step_csv(csv_path, steps)
    report["report_path"] = str(report_path)
    report["summary_csv_path"] = str(csv_path)
    if breaking_point:
        print(f"最佳并发数: {last_healthy if last_healthy is not None else '无达标阶梯'}")
    else:
        print(f"最佳并发数: {last_healthy}（当前测试范围内）")
    if failed_records or gateway_failure_records:
        print(f"业务/网关失败详情已保存: {request_error_path}")
    return report


def _metric_self_test() -> dict[str, Any]:
    def network_call(status: int) -> dict[str, Any]:
        return {
            "status_code": status,
            "error": None if status == 200 else f"HTTP {status}",
            "network_total_elapsed_ms": 100.0,
            "headers_elapsed_ms": 60.0,
            "ttfb_ms": 80.0,
            "response_transfer_ms": 20.0,
        }

    def request(
        number: int,
        burst_id: int,
        start: float,
        finish: float,
        success: bool,
    ) -> dict[str, Any]:
        return {
            "request_number": number,
            "burst_id": burst_id,
            "sent": True,
            "send_perf": start,
            "business_finished_perf": finish,
            "inflight_started_perf": start + 0.01,
            "inflight_finished_perf": finish - 0.01,
            "success": success,
            "error": None if success else "HTTP 500",
            "error_category": None if success else "HTTP error",
            "business_wall_elapsed_ms": (finish - start) * 1000,
            "first_byte_ms": 80.0 if success else None,
            "prompt_tokens": 10 if success else None,
            "completion_tokens": 20 if success else None,
            "total_tokens": 30 if success else None,
            "network_total_elapsed_ms": 100.0,
            "response_transfer_ms": 20.0 if success else None,
            "request_bytes": 100,
            "response_bytes": 200 if success else 20,
            "network_calls": [network_call(200 if success else 500)],
        }

    # Two 2-second bursts separated by 2 seconds: measurement window=6s,
    # active burst sum=4s. This detects the former inflated-QPS denominator.
    results = [
        request(1, 1, 10.0, 12.0, True),
        request(2, 1, 10.1, 11.0, True),
        request(3, 2, 14.0, 15.0, False),
        request(4, 2, 14.1, 16.0, True),
    ]
    summary = _summarize_step(2, 2, 7.0, 2, results)
    assert summary["measurement_duration_seconds"] == 6.0
    assert summary["active_burst_duration_seconds"] == 4.0
    assert summary["throughput"]["total_qps"] == round(4 / 6, 6)
    assert summary["throughput"]["success_qps"] == 0.5
    assert summary["throughput"]["active_burst_total_qps"] == 1.0
    assert summary["full_concurrency_bursts"] == 2
    assert summary["gateway_call_metrics_ms"]["network_total_elapsed"]["sample_count"] == 3
    assert summary["errors"]["gateway_success_count"] == 3
    assert summary["errors"]["gateway_failure_count"] == 1
    assert len(summary["failed_request_records"]) == 1
    assert len(summary["gateway_failure_records"]) == 1
    assert summary["gateway_failure_records"][0]["status_code"] == 500
    assert summary["token_usage"]["output_token_throughput"] == 10.0
    assert _percentile([10.0, 20.0, 30.0, 40.0], 95) == 38.5

    with tempfile.TemporaryDirectory(prefix="ai_interview_metric_test_") as temp_dir:
        error_path = Path(temp_dir) / "errors.json"
        _write_concurrency_errors(
            error_path, "campus", "profile",
            summary["failed_request_records"], summary["gateway_failure_records"],
        )
        error_report = json.loads(error_path.read_text(encoding="utf-8"))
        assert error_report["failed_business_request_count"] == 1
        assert error_report["failed_gateway_call_count"] == 1
        csv_path = Path(temp_dir) / "summary.csv"
        _write_step_csv(csv_path, [summary])
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            csv_row = next(csv.DictReader(handle))
        assert csv_row["success_count"] == "3"
        assert csv_row["failed_count"] == "1"
        assert csv_row["gateway_failure_count"] == "1"

    capture = _CallbackCapture()
    capture.put("profile", "r1", {"ok": True})
    capture.put("profile", "r2", {"ok": False})
    assert len(capture.all()) == 2
    assert capture.take("profile", "r1")["payload"] == {"ok": True}
    assert len(capture.all()) == 1
    return {
        "ok": True,
        "measurement_duration_seconds": summary["measurement_duration_seconds"],
        "active_burst_duration_seconds": summary["active_burst_duration_seconds"],
        "total_qps": summary["throughput"]["total_qps"],
        "success_qps": summary["throughput"]["success_qps"],
    }


def _self_test() -> dict[str, Any]:
    runtime = _extract_business_package()
    _verify_original_sources(runtime)
    fixture_results = {}
    for (recruit, task), raw in EMBEDDED_FIXTURES_JSON.items():
        data = json.loads(raw)
        resume_id, adapted, recruit_type = _adapt_request(data, recruit, task)
        fixture_results[f"{recruit}/{task}"] = {
            "resume_id": resume_id, "recruit_type": recruit_type,
            "input_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "adapted_is_object": isinstance(adapted, dict),
        }
    # Import every production module without contacting the network.
    runtime_text = str(runtime)
    if runtime_text not in sys.path:
        sys.path.insert(0, runtime_text)
    strategy_module: Any = importlib.import_module("inference.strategy")
    get_strategy = strategy_module.get_strategy
    assert get_strategy(1).type_code == "campus"
    assert get_strategy(31).type_code == "internal"
    importlib.import_module("inference.analyze_service")
    json_repair_module: Any = importlib.import_module("json_repair")
    repaired = json_repair_module.repair_json("{'ok': true}")
    assert json.loads(repaired)["ok"] is True
    return {
        "ok": True, "production_revision": PRODUCTION_REVISION,
        "package_sha256": BUSINESS_PACKAGE_SHA256,
        "verified_original_files": len(ORIGINAL_SOURCE_HASHES),
        "metric_checks": _metric_self_test(),
        "fixtures": fixture_results,
    }


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="生产业务等价的校招/内招阶梯式并发模型测试工具")
    parser.add_argument("--recruit", default="both", help="campus | internal | both")
    parser.add_argument("--task", default="profile", help="profile | questions | report | all")
    parser.add_argument("--fixture", help="自定义 JSON，仅可用于单一招聘类型和任务")
    parser.add_argument("--customer-code", "--app-key", dest="customer_code",
                        default=_env("AI_GATEWAY_CUSTOMER_CODE", DEFAULT_CONFIG["customer_code"]))
    parser.add_argument("--secret-key", default=_env("AI_GATEWAY_SECRET_KEY", DEFAULT_CONFIG["secret_key"]))
    parser.add_argument("--url", "--api-url", dest="url",
                        default=_env("AI_GATEWAY_URL", DEFAULT_CONFIG["url"]))
    parser.add_argument("--model", default=_env("AI_GATEWAY_MODEL", DEFAULT_CONFIG["model"]))
    parser.add_argument("--component-code", default=_env("AI_GATEWAY_COMPONENT_CODE", DEFAULT_CONFIG["component_code"]))
    parser.add_argument("--timeout", type=int, default=int(_env("AI_GATEWAY_TIMEOUT", DEFAULT_CONFIG["timeout"])))
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--max-tokens", type=int, default=None)
    parser.add_argument("--verify-ssl", action="store_true")
    parser.add_argument("--skip-ping", action="store_true", help="跳过每项业务前的网关探测")
    parser.add_argument("--ping-only", action="store_true")
    parser.add_argument("--self-test", action="store_true", help="离线校验业务包、模块和六份输入")
    parser.add_argument(
        "--concurrency-levels", default="1,2,4,8,16,20,25,30,40,50,60,70,80,90,100",
        help="逗号分隔的并发阶梯，默认 1,2,4,8,16,20,25,30,40,50,60,70,80,90,100",
    )
    parser.add_argument("--rounds", type=int, default=3, help="每个并发阶梯的同步批次数，默认 3")
    parser.add_argument(
        "--success-threshold", type=float, default=95.0,
        help="成功率低于该百分比时停止，默认 95；等于阈值时继续",
    )
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    return parser.parse_args(argv)


def _parse_concurrency_levels(value: str) -> list[int]:
    try:
        levels = [int(item.strip()) for item in str(value).split(",") if item.strip()]
    except ValueError as exc:
        raise ValueError("--concurrency-levels 必须是逗号分隔的正整数") from exc
    if not levels or any(level <= 0 for level in levels):
        raise ValueError("--concurrency-levels 至少包含一个正整数")
    if levels != sorted(set(levels)):
        raise ValueError("--concurrency-levels 必须严格递增且不能重复")
    return levels


def _expand_recruits(value: str) -> list[str]:
    value = str(value).strip().lower()
    if value in ("both", "all"):
        return ["campus", "internal"]
    if value in ("campus", "1"):
        return ["campus"]
    if value in ("internal", "31"):
        return ["internal"]
    raise ValueError(f"不支持的 recruit：{value}")


def _expand_tasks(value: str) -> list[str]:
    value = str(value).strip().lower()
    aliases = {"profile": "profile", "questions": "questions", "question": "questions",
               "gqa": "questions", "report": "report"}
    if value == "all":
        return ["profile", "questions", "report"]
    if value in aliases:
        return [aliases[value]]
    raise ValueError(f"不支持的 task：{value}")


def main(argv=None) -> int:
    args = _parse_args(argv)
    if args.self_test:
        print(json.dumps(_self_test(), ensure_ascii=False, indent=2))
        return 0
    try:
        recruits = _expand_recruits(args.recruit)
        tasks = _expand_tasks(args.task)
        levels = _parse_concurrency_levels(args.concurrency_levels)
        if args.fixture and (len(recruits) != 1 or len(tasks) != 1):
            raise ValueError("--fixture 只能与单一 --recruit 和单一 --task 一起使用")
        if args.timeout <= 0 or not 0 <= args.temperature <= 2:
            raise ValueError("timeout 必须大于0，temperature 必须在0到2之间")
        if args.rounds <= 0:
            raise ValueError("--rounds 必须大于 0")
        if not 0 <= args.success_threshold <= 100:
            raise ValueError("--success-threshold 必须在 0 到 100 之间")
        required = (args.customer_code, args.secret_key, args.url, args.model, args.component_code)
        if not all(str(value).strip() for value in required):
            raise ValueError("网关配置不完整")
    except ValueError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return 2

    config = {
        "customer_code": args.customer_code.strip(), "secret_key": args.secret_key.strip(),
        "url": args.url.strip(), "model": args.model.strip(),
        "component_code": args.component_code.strip(), "timeout": args.timeout,
        "verify_ssl": args.verify_ssl, "max_tokens": args.max_tokens,
    }
    out_dir = Path(args.out_dir).expanduser()
    if not out_dir.is_absolute():
        out_dir = (ROOT / out_dir).resolve()
    if args.ping_only:
        try:
            _, qwen_client, _ = _load_business_runtime(config)
            result = _ping(qwen_client, config["timeout"])
            path = out_dir / _safe_name(config["model"]) / "ping.json"
            _write_json(path, {"gateway": _public_config(config), "ping": result})
            print(f"网关可用：{result['elapsed_sec']:.2f}s -> {path}")
            return 0
        except Exception:
            path = out_dir / _safe_name(config["model"]) / "ping.error.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(traceback.format_exc(), encoding="utf-8")
            print(f"网关探测失败 -> {path}", file=sys.stderr)
            return 1

    reports = []
    for recruit in recruits:
        for task in tasks:
            try:
                with _network_probe():
                    report = _run_staircase(
                        config, recruit, task, args.fixture, args.temperature, levels,
                        args.rounds, args.success_threshold, out_dir, args.skip_ping,
                    )
                reports.append(report)
            except Exception:
                error_text = traceback.format_exc()
                error_path = (
                    out_dir / _safe_name(config["model"]) /
                    f"{recruit}_{task}_concurrency.error.txt"
                )
                error_path.parent.mkdir(parents=True, exist_ok=True)
                error_path.write_text(error_text, encoding="utf-8")
                print(
                    f"[{recruit}/{task}] 并发测试异常: "
                    f"{_short_error(error_text.splitlines()[-1] if error_text.splitlines() else error_text)}；"
                    f"完整错误 -> {error_path}",
                    file=sys.stderr,
                )

    successful_requests = sum(
        step["success_count"] for report in reports for step in report["steps"]
    )
    return 0 if len(reports) == len(recruits) * len(tasks) and successful_requests > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
