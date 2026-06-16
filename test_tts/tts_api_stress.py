import numpy as np
import pytest
import threading
import concurrent.futures
import statistics
import time
import json
import os
import wave
from datetime import datetime
from conftest import (
    tts_request_with_sync,
    OUTPUT_DIR,
    CONCURRENT_WORKERS,
    CONCURRENT_REQUESTS,
    STRESS_TEST_COUNT
)


class TestTTSPerformance:
    """TTS性能测试类"""

    @pytest.mark.performance
    @pytest.mark.slow
    def test_9_并发性能(self, tts_url, perf_stats):
        """指标9：并发请求性能专项测试 - 提高标准"""
        print("\n[并发性能测试]")

        def single_request(request_id):
            payload = {
                "model": "instruct2",
                "input": f"并发测试请求_{request_id}",
                "tts_params": {
                    "instruct_text": "You are a helpful assistant. 很自然地说<|endofprompt|>",
                    "prompt_audio": "kehu_female_b",
                    "zero_shot_spk_id": "kehu_female_b",
                    "speed": 1.0,
                    "stream": True
                }
            }
            return tts_request_with_sync(tts_url, payload, enable_playback=False)

        print(f"线程数: {CONCURRENT_WORKERS}, 请求数: {CONCURRENT_REQUESTS}")

        results = []
        failed_requests = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENT_WORKERS) as executor:
            futures = [executor.submit(single_request, i) for i in range(CONCURRENT_REQUESTS)]

            for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
                try:
                    result = future.result(timeout=30)
                    results.append(result)
                    if i % 10 == 0 or i == CONCURRENT_REQUESTS:
                        print(f"进度: {i}/{CONCURRENT_REQUESTS}")
                except Exception as e:
                    failed_requests.append(f"请求{i}: {str(e)[:50]}")

        if failed_requests:
            print(f"失败请求: {len(failed_requests)}个")
            for req in failed_requests[:3]:  # 只显示前3个失败原因
                print(f"  - {req}")

        if results:
            ttfts = [r["ttft"] for r in results]
            rts = [r["rt"] for r in results]

            perf_stats["TTFT"].extend(ttfts)
            perf_stats["RT"].extend(rts)

            success_rate = len(results) / CONCURRENT_REQUESTS

            print(f"📊 并发测试结果:")
            print(f"  成功: {len(results)}/{CONCURRENT_REQUESTS} (成功率={success_rate:.1%})")
            print(f"  TTFT平均: {statistics.mean(ttfts):.3f}s, P95: {sorted(ttfts)[int(len(ttfts) * 0.95)]:.3f}s")
            print(f"  RT平均: {statistics.mean(rts):.3f}s, P95: {sorted(rts)[int(len(rts) * 0.95)]:.3f}s")

            # 🔥 提高标准：成功率从80%提升到90%
            assert success_rate >= 0.90, f"并发成功率过低: {success_rate:.1%} < 90%"

            # 🔥 新增：P95性能断言
            ttft_p95 = sorted(ttfts)[int(len(ttfts) * 0.95)]
            rt_p95 = sorted(rts)[int(len(rts) * 0.95)]
            assert ttft_p95 <= 0.5, f"TTFT P95过高: {ttft_p95:.3f}s > 0.5s"
            assert rt_p95 <= 3.0, f"RT P95过高: {rt_p95:.3f}s > 3.0s"

            print(f"✅ 并发性能通过: 成功率={success_rate:.1%}, TTFT_P95={ttft_p95:.3f}s, RT_P95={rt_p95:.3f}s")

    @pytest.mark.performance
    def test_10_长文本稳定性(self, tts_url, perf_stats):
        """指标10：长文本转换稳定性专项测试"""
        print("\n[长文本测试]")

        base_text = "在我居住的这座城市，春天并不总是从花开的颜色开始。更多时候，它始于一场细微却顽固的声响——是屋檐下冰柱断裂的第一声脆响，是暖风挤过老旧窗框时发出的低啸，是清晨五点钟，窗外那棵白杨树上的麻雀突然换了唱法。我住在城北一栋九十年代建成的居民楼里，六层，没电梯。每年二月底到三月初，是我的耳朵最敏感的季节。因为视觉往往欺骗人：日历上说立春已过，可望出去依然是灰蒙蒙的天，光秃秃的枝。但只要把耳朵朝向窗外，就能听见变化。先是供暖管道里的水声变得绵软了。深冬时，那声音像老人干咳，一下一下撞击着铁管；而到了雨水节气前后，水流声忽然拖出了尾巴，仿佛冻僵的手指渐渐恢复知觉，开始缓慢地揉搓。邻居张大爷说这叫“水醒了”。他是个退休的锅炉工，耳朵比眼睛好使。有一年惊蛰那天，他趴在走廊的暖气片上听了半晌，对我说：“暖气管道里开始跑春汛了，铁皮都跟着哼。”然后是小贩的叫卖声。冬天，楼下的早点摊贩缩在棉大衣里，嗓音发闷，像裹了好几层保鲜膜。三月的某个早晨，卖豆腐脑的大姐突然揭掉了防风罩，声音亮出来了：“豆——腐——脑——”那个“脑”字拖得长长的，像解冻的河流拐过第一个弯。我躺在床上，听见这声吆喝穿过尚未全开的窗缝，心里就知道：毛衣可以少穿一件了。春天的声音是逐渐增厚的。三月中旬，对街的小学开学了，操场上的跑步声、跳绳拍打地面的啪啪声、孩子们尖细的笑声，一层层铺上来。施工队也复工了，远处传来打桩机的闷响，虽说有些粗暴，却也宣告着这座城市的筋骨开始活动。而我最喜欢的是傍晚六点左右，楼上楼下几乎同时响起菜刀碰砧板的笃笃声——那是家家户户在切春韭或荠菜，声音既脆又韧，像刚冒头的草芽。到了四月，雨的声响变了味道。冬雨是噼噼啪啪砸下来的，夹着冰粒，听着就让人缩脖子。春雨呢，落下来是“沙沙沙”的，很轻很密，打在楼下那排香樟树上，像有人用毛笔在一大张宣纸上不停地划。雨停之后，屋檐滴水的节奏也跟冬天不同——冬滴是僵硬的，一下是一下；春滴则带着弹性，滴答……滴答滴答……忽然连成一小串，像是水滴在练习跑跳步。我渐渐养成一个习惯：每个春天的早晨，先不开灯，闭眼听两分钟。听汽车轮胎驶过湿漉漉路面的声音——冬天是刺耳的刮擦声，春天则变成柔和的唰唰声。听风摇动去年残留的枯叶——枯叶碰枯叶，发出干燥的咔咔声，但风一过去，底下就会透出新叶那种几乎听不见的呼吸般的微响。春天教会我一件事：声音也是有质感和温度的。解冻的声响不是一下完成的，它是从铁管里的水声、小贩的吆喝、砧板上的刀声、雨点的沙沙声中，一点一点渗出来的。当所有声音都变得柔软而富有弹性时，我就知道，漫长的冬天确实过去了。如果说春天的声音是在积累和破土，那么夏天的声音就是一场盛大的满溢。这座城市的夏天来得猛，常常是五月中旬某一个午后，气温毫无征兆地蹿到三十度以上，蝉声也随之炸开。蝉是这座城市夏天的首席乐手。它们不单独出场，一开口就是大合唱。小区里的柳树、槐树、杨树，但凡有枝叶的地方，都被蝉占据。它们的叫声不像音乐，更像一种白噪音——高亢、持续、几乎无止无休。第一次听见时觉得烦，听了三天以后反而离不开。有一年空调坏了，我开着窗睡觉，整夜被蝉声包裹，竟睡得比平时还沉。大概因为蝉声太均匀，均匀到大脑把它当成了沉默的背景。但是蝉声也有变化。清晨的蝉声是试探性的，稀稀拉拉的几声，像指挥抬起手臂还没落下的瞬间。到上午十点，太阳升高，蝉声便随之一浪高过一浪，直至正午到达顶峰。那时你要是站在树底下，耳朵里只剩下一片嗡嗡的轰鸣，连近在咫尺的人说话都要喊。午后雷阵雨来临前，蝉声会突然集体沉默，一两分钟的死寂，比任何预报都准。等雨下过，蝉声重新响起，带着水汽，比先前低了三成音量，像被洗过一遍。除了蝉，夏天还有另一种标志性声音——空调外机的嗡嗡声。这座城市有几百万台空调，每个夏天同时运转。入夜以后，你走在小区里，两边的墙壁上挂满了嗡嗡作响的铁盒子，像密密麻麻的马蜂窝。这声音不高，但有一种无孔不入的渗透力。我住六楼，楼下五户人家的外机声音各不相同：一楼的像哮喘，二楼的像电剃须刀，三楼的带一点金属颤音，四楼的最安静，五楼的每隔十五分钟会有一声类似叹息的起伏。这些声音混杂在一起，织成一张低音网，把整栋楼兜在里头。但夏天真正的魅力，在于它藏在燥热里的那些静。正午最热的时候，整个小区会陷入一种奇迹般的安静。蝉声似乎被热浪烫平了，汽车极少经过，连狗都趴在瓷砖上喘气，不出声。我坐在书桌前，汗沿着脊背往下淌，耳朵里却能听见很细微的东西：墙上挂钟秒针的跳动、自己喉咙吞咽口水的声音、书页翻动时纤维摩擦的沙沙声。这种静不是空的，它是被热量压实的、沉甸甸的静，仿佛整个城市都进入了午睡的短暂休克。黄昏是声音的狂欢节。六点以后，暑气稍退，人声重新冒出来。楼下棋摊的象棋砸在石板上的脆响，老头们争论每一步棋的嚷嚷声，广场舞的音响从远处飘来——先是凤凰传奇，后来又换成网络神曲，低音炮震得地面微微发颤。七岁的小孩子在花坛边骑小自行车，车铃铛叮铃叮铃响个没完。还有烧烤摊的抽风机，呼呼地转着，把孜然和辣椒的气味连同噪音一起吹到半条街上。夏天的夜晚，声音一直要延续到凌晨。我有时候失眠，会趴在窗台上听。凌晨一点钟，最后一桌酒席散了，醉汉的脚步声在空荡荡的马路上显得特别响，一步一步，像踩在鼓面上。两点钟，野猫叫春，婴儿啼哭般的嗓音在楼宇间来回弹射。三点钟，第一辆垃圾清运车轰隆隆开进来，垃圾桶被掀翻、扣击、放回，金属碰撞声能传出一公里远。然后四点，天边发白，第一批鸟醒了，试了试嗓子，开始一小节一小节的吟唱。我后来想，夏天的声音之所以让人难忘，或许正因为它不拒绝任何东西——它把蝉鸣、空调、打鼾、争吵、猫叫、车声、雨声、所有的燥与静，全部搅在一起，像一锅滚烫的杂烩汤。你没法挑拣，只能一头扎进去，然后在某一个瞬间，突然发现自己也成了这声音的一部分。秋天是声音的下降音阶。如果说夏天是一百个喇叭齐鸣，那么秋天就是那个调音师慢慢地把音量旋钮往回拧。九月中旬，第一场秋风过后，蝉声突然少了一大半，剩下的也在苟延残喘，叫声变得沙哑，断断续续，像卡了带的录音机。我开始注意到那些在夏天被遮盖的声音。比如风。夏天的风没有形状，只是热烘烘地拂过去；秋风却是有纹理的。它穿过树叶时，叶子已经半黄半干，发出的不再是沙沙声，而是哗啦哗啦的脆响，像有人在不远处翻一本厚厚的旧书。风再大一些的时候，整棵树的叶子一起摇，声音便有了层次：外层的叶子是清亮的干响，内层的叶子还带一点潮气，音调更低更钝。我站在树下听过一次，竟听出了和声的意味。落叶的声音比许多人想象的要大。深秋的早晨，清洁工还没来扫，人行道上铺满了法国梧桐的枯叶。你踩上去，“咔嚓”一声，干脆利落，像踩碎了薄脆饼干。如果快步走过，身后会留下一连串细碎的爆裂声。有时候一阵风过来，枯叶贴着地面滑行，发出“嗤嗤嗤”的长音，像是很多只小手在水泥地上匆忙地写字。秋天也是声音变得遥远的季节。夏天的声音是扑面而来的——蝉就在你窗外，烧烤摊就在你楼下。可到了秋天，隔壁工地打桩的声音好像退了五十米，楼下小孩哭闹的声音像是隔着两层棉被传上来。我查过资料，这跟空气的温度和湿度有关，但更愿意相信是秋天自带的一种疏离感——它把所有事物都推远了一点，给你腾出些空隙来。雨声在秋天换了一种质地。秋雨不再是夏天那种倾盆而下、砸得雨棚砰砰作响的雨，而是细细的、绵密的、不急不躁的雨。它打在窗玻璃上是“啪”的一声轻响，然后拉成一道水痕流下去。如果下一整夜，你躺在床上听，会感觉那声音不是在窗外，而是在脑子里——像有人在你脑海里用一支极细的毛笔，一下一下、不厌其烦地点染。十月底到十一月，候鸟过境是这个城市秋天最奇特的声景。我住在六楼，正好和树冠齐平。某个凌晨，大概四五点钟，我被一阵声音吵醒——不是噪音，而是一种密集的、急促的、翅膀扇动和短促鸣叫混合的声音。我爬起来往外看，天还没亮，什么都看不见，但那声音铺天盖地，像一条河流从头顶经过。持续了十几分钟才渐渐远去。后来邻居告诉我，那是椋鸟和斑鸫的迁徙群。之后每年的那几天，我都会在凌晨醒来，侧耳等着那条声音的河流。秋分以后，白昼变短，声音的时段也跟着转移。夏天的声音高峰在正午和傍晚，秋天的高峰则在清晨和黄昏薄暮时分。清晨六点，天刚灰亮，鸟鸣是主角——不是春天那种热烈的求偶鸣唱，而是短促的联络声，啾、啾、啾，像在互相确认位置。黄昏五点半，天黑得早，家家户户关窗的声音此起彼伏——哐、哐、哐，窗户合上，把秋凉挡在外面，也把屋里的人声关了起来。然后整个夜晚变得很安静，安静到你能听见自己的心跳，以及暖气片试水时咕噜咕噜的气泡声。秋天声音中最不起眼却最动人的，大概是晾晒的声音。这个季节阳光好，又不暴烈，阳台上挂满了被子、褥子、厚衣服。风一吹，床单呼啦呼啦飘起来，衣架互相碰击，叮叮当当的，像一串不规律的风铃。楼下的老太太拍打棉被，“嘭、嘭、嘭”，沉闷而有节奏，每一下都带起一团细细的灰尘，在阳光里闪着光。我在秋天养成了傍晚散步的习惯，不为别的，就为听听脚下枯叶的破碎声。那种声音有一种决绝的美——它宣告了繁盛的终结，却把终结本身变成了清脆悦耳的仪式。走到十一月下旬，树上几乎没什么叶子了，风直接吹过光秃秃的枝干，发出呜咽般的哨音。这时候我知道，秋天要走了，它把音量旋钮拧到了最小，准备交接给冬天。"
        long_text = base_text * 10
        print(f"文本长度: {len(long_text)}字符")

        payload = {
            "model": "instruct2",
            "input": long_text,
            "tts_params": {
                "instruct_text": "You are a helpful assistant. 很自然地说<|endofprompt|>",
                "prompt_audio": "kehu_female_b",
                "zero_shot_spk_id": "kehu_female_b",
                "speed": 1.0,
                "stream": True
            }
        }

        res = tts_request_with_sync(tts_url, payload, enable_playback=False)
        file_size = os.path.getsize(res["path"])
        perf_stats["TTFT"].append(res["ttft"])
        perf_stats["RT"].append(res["rt"])

        with wave.open(res["path"], 'rb') as wav:
            frames = wav.getnframes()
            rate = wav.getframerate()
            duration = frames / float(rate)

        print(f"TTFT: {res['ttft']:.3f}s, RT: {res['rt']:.3f}s")
        print(f"音频时长: {duration:.2f}s, 大小: {file_size / 1024:.1f}KB")

        assert file_size > len(long_text) * 10
        print("通过")

    @pytest.mark.performance
    # 修正压力测试阈值
    def test_11_压力测试(self, tts_url, perf_stats):
        """指标11：持续压力测试专项测试 - 收紧标准差阈值"""
        print("\n[压力测试]")

        test_count = STRESS_TEST_COUNT
        print(f"请求数: {test_count}")

        ttfts, rts = [], []
        for i in range(test_count):
            text = f"压力测试_{i + 1}"
            payload = {
                "model": "instruct2",
                "input": text,
                "tts_params": {
                    "instruct_text": "You are a helpful assistant. 很自然地说<|endofprompt|>",
                    "prompt_audio": "kehu_female_b",
                    "zero_shot_spk_id": "kehu_female_b",
                    "speed": 1.0,
                    "stream": True
                }
            }

            res = tts_request_with_sync(tts_url, payload, enable_playback=False)
            ttfts.append(res["ttft"])
            rts.append(res["rt"])

            if (i + 1) % 5 == 0 or i == test_count - 1:
                print(f"进度: {i + 1}/{test_count}")

            if i < test_count - 1:
                time.sleep(0.5)

        perf_stats["TTFT"].extend(ttfts)
        perf_stats["RT"].extend(rts)

        ttft_mean = statistics.mean(ttfts)
        rt_mean = statistics.mean(rts)
        ttft_std = statistics.stdev(ttfts) if len(ttfts) > 1 else 0
        rt_std = statistics.stdev(rts) if len(rts) > 1 else 0

        print(f"📊 压力测试结果:")
        print(f"  TTFT: 平均{ttft_mean:.3f}s, 标准差{ttft_std:.3f}s, 范围[{min(ttfts):.3f}s~{max(ttfts):.3f}s]")
        print(f"  RT: 平均{rt_mean:.3f}s, 标准差{rt_std:.3f}s, 范围[{min(rts):.3f}s~{max(rts):.3f}s]")

        # 🔥 收紧标准差阈值
        if len(ttfts) > 1 and len(rts) > 1:
            assert ttft_std <= 0.05, f"TTFT标准差过大: {ttft_std:.3f}s > 0.05s"  # 从0.1收紧到0.05
            assert rt_std <= 0.1, f"RT标准差过大: {rt_std:.3f}s > 0.1s"  # 从0.2收紧到0.1

        # 🔥 新增：绝对性能阈值
        assert max(ttfts) <= 1.0, f"TTFT最大值过高: {max(ttfts):.3f}s > 1.0s"
        assert max(rts) <= 4.0, f"RT最大值过高: {max(rts):.3f}s > 4.0s"

        print("✅ 压力测试通过")

    @pytest.mark.performance
    def test_12_参数组合测试(self, tts_url, perf_stats):
        """指标12：不同参数组合兼容性专项测试"""
        print("\n[参数组合测试]")

        test_cases = [
            {"speed": 0.8, "spk_id": "kehu_female_b"},
            {"speed": 1.0, "spk_id": "kehu_female_b"},
            {"speed": 1.2, "spk_id": "kehu_female_b"},
            {"speed": 1.0, "spk_id": "yingyeyuan_male"},
        ]

        all_ttfts, all_rts = [], []
        for i, case in enumerate(test_cases, 1):
            payload = {
                "model": "instruct2",
                "input": "测试不同参数组合下的语音合成效果",
                "tts_params": {
                    "instruct_text": "You are a helpful assistant. 很自然地说<|endofprompt|>",
                    "prompt_audio": case["spk_id"],
                    "zero_shot_spk_id": case["spk_id"],
                    "speed": case["speed"],
                    "stream": True
                }
            }

            res = tts_request_with_sync(tts_url, payload, enable_playback=False)
            all_ttfts.append(res["ttft"])
            all_rts.append(res["rt"])
            print(f"组合{i}: 语速{case['speed']}, 声线{case['spk_id']}, TTFT={res['ttft']:.3f}s")

        perf_stats["TTFT"].extend(all_ttfts)
        perf_stats["RT"].extend(all_rts)

        print(f"TTFT范围: {min(all_ttfts):.3f}s~{max(all_ttfts):.3f}s")
        print(f"RT范围: {min(all_rts):.3f}s~{max(all_rts):.3f}s")
        assert len(all_ttfts) == len(test_cases)
        print("通过")

    def test_13_生成性能报告(self, perf_stats):
        """指标13：生成完整性能测试报告"""
        print("\n[生成报告]")

        if not perf_stats["TTFT"] or not perf_stats["RT"]:
            pytest.skip("无性能数据")

        report = {
            "测试时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "总测试次数": len(perf_stats["TTFT"]),
            "性能指标": {
                "TTFT_首包延迟_秒": {
                    "样本数": len(perf_stats["TTFT"]),
                    "平均值": float(round(np.mean(perf_stats["TTFT"]), 3)),
                    "P50": float(round(np.percentile(perf_stats["TTFT"], 50), 3)),
                    "P90": float(round(np.percentile(perf_stats["TTFT"], 90), 3)),
                    "P95": float(round(np.percentile(perf_stats["TTFT"], 95), 3)),
                    "P99": float(round(np.percentile(perf_stats["TTFT"], 99), 3)),
                    "最大值": float(round(max(perf_stats["TTFT"]), 3)),
                    "最小值": float(round(min(perf_stats["TTFT"]), 3)),
                    "标准差": float(round(np.std(perf_stats["TTFT"]), 3)),
                },
                "RT_合成总耗时_秒": {
                    "样本数": len(perf_stats["RT"]),
                    "平均值": float(round(np.mean(perf_stats["RT"]), 3)),
                    "P50": float(round(np.percentile(perf_stats["RT"], 50), 3)),
                    "P90": float(round(np.percentile(perf_stats["RT"], 90), 3)),
                    "P95": float(round(np.percentile(perf_stats["RT"], 95), 3)),
                    "P99": float(round(np.percentile(perf_stats["RT"], 99), 3)),
                    "最大值": float(round(max(perf_stats["RT"]), 3)),
                    "最小值": float(round(min(perf_stats["RT"]), 3)),
                    "标准差": float(round(np.std(perf_stats["RT"]), 3)),
                }
            },
        }

        report_filename = f"TTS性能报告_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        report_path = os.path.join(OUTPUT_DIR, report_filename)

        with open(report_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)

        print(f"报告生成: {report_path}")
        print(f"TTFT P95: {report['性能指标']['TTFT_首包延迟_秒']['P95']}s")
        print(f"RT P95: {report['性能指标']['RT_合成总耗时_秒']['P95']}s")
        assert os.path.exists(report_path)
        print("通过")


class TestTTSExceptionHandling:
    """TTS异常处理测试"""

    def test_14_异常输入处理(self, tts_url):
        """指标14：异常输入处理能力测试"""
        print("\n[异常处理测试]")

        test_cases = [
            {"name": "空文本", "text": ""},
            {"name": "超长文本", "text": "A" * 10000},
            {"name": "特殊字符", "text": "测试\x00空字符和\n换行符"},
            {"name": "纯数字", "text": "1234567890"},
            {"name": "混合内容", "text": "Hello 123 测试！@#$%"},
        ]

        passed, failed, skipped = 0, 0, 0
        for case in test_cases:
            try:
                payload = {
                    "model": "instruct2",
                    "input": case["text"],
                    "tts_params": {
                        "instruct_text": "You are a helpful assistant. 很自然地说<|endofprompt|>",
                        "prompt_audio": "kehu_female_b",
                        "zero_shot_spk_id": "kehu_female_b",
                        "speed": 1.0,
                        "stream": True
                    }
                }

                res = tts_request_with_sync(tts_url, payload, enable_playback=False)
                if os.path.exists(res["path"]) and os.path.getsize(res["path"]) > 0:
                    passed += 1
                else:
                    skipped += 1
            except Exception as e:
                error_msg = str(e)
                if "状态码" in error_msg or "失败" in error_msg:
                    passed += 1
                else:
                    failed += 1

        print(f"结果: 通过{passed}, 失败{failed}, 跳过{skipped}")
        assert failed == 0


class TestTTSAdvancedPerformance:
    """TTS高级性能测试"""

    # 为阶梯压力测试增加断言
    def test_19_阶梯式压力测试(self, tts_url, perf_stats):
        """指标19：阶梯式压力测试 - 增加性能衰减断言"""
        print("\n[阶梯压力测试]")

        stages = [
            {"concurrent": 2, "duration": 3, "name": "低负载"},
            {"concurrent": 5, "duration": 3, "name": "中负载"},
            {"concurrent": 8, "duration": 3, "name": "高负载"},
            {"concurrent": 2, "duration": 3, "name": "恢复"},
        ]

        stage_results = []
        for stage in stages:
            def single_request(request_id):
                payload = {
                    "model": "instruct2",
                    "input": f"压力测试_{request_id}",
                    "tts_params": {
                        "instruct_text": "You are a helpful assistant. 用自然的语气说<|endofprompt|>",
                        "prompt_audio": "kehu_female_b",
                        "zero_shot_spk_id": "kehu_female_b",
                        "speed": 1.0,
                        "stream": True
                    }
                }
                return tts_request_with_sync(tts_url, payload, enable_playback=False)

            results = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=stage["concurrent"]) as executor:
                end_time = time.time() + stage["duration"]
                request_id = 0
                while time.time() < end_time:
                    futures = [executor.submit(single_request, request_id + i) for i in range(stage["concurrent"])]
                    request_id += stage["concurrent"]
                    for future in concurrent.futures.as_completed(futures):
                        try:
                            result = future.result(timeout=30)
                            results.append(result)
                        except:
                            pass

            if results:
                ttfts = [r["ttft"] for r in results]
                rts = [r["rt"] for r in results]
                stage_results.append({
                    "stage": stage["name"],
                    "requests": len(results),
                    "avg_ttft": statistics.mean(ttfts) if ttfts else 0,
                    "p95_ttft": sorted(ttfts)[int(len(ttfts) * 0.95)] if len(ttfts) >= 20 else 0,
                    "avg_rt": statistics.mean(rts) if rts else 0,
                    "p95_rt": sorted(rts)[int(len(rts) * 0.95)] if len(rts) >= 20 else 0,
                })

        # 🔥 新增：阶段性能对比断言
        if len(stage_results) >= 2:
            low_load = stage_results[0]  # 低负载阶段
            high_load = stage_results[2] if len(stage_results) > 2 else stage_results[1]  # 高负载阶段

            print(f"📊 阶梯压力测试对比:")
            print(f"  低负载({low_load['stage']}): TTFT={low_load['avg_ttft']:.3f}s, RT={low_load['avg_rt']:.3f}s")
            print(f"  高负载({high_load['stage']}): TTFT={high_load['avg_ttft']:.3f}s, RT={high_load['avg_rt']:.3f}s")

            # 计算性能衰减
            ttft_degradation = (high_load['avg_ttft'] - low_load['avg_ttft']) / low_load['avg_ttft'] if low_load[
                                                                                                            'avg_ttft'] > 0 else 0
            rt_degradation = (high_load['avg_rt'] - low_load['avg_rt']) / low_load['avg_rt'] if low_load[
                                                                                                    'avg_rt'] > 0 else 0

            print(f"  性能衰减: TTFT={ttft_degradation:.1%}, RT={rt_degradation:.1%}")

            # 🔥 断言：高负载下性能衰减不超过50%
            assert ttft_degradation <= 0.5, f"高负载下TTFT衰减过大: {ttft_degradation:.1%} > 50%"
            assert rt_degradation <= 0.5, f"高负载下RT衰减过大: {rt_degradation:.1%} > 50%"

        for result in stage_results:
            print(
                f"{result['stage']}: {result['requests']}请求, TTFT={result['avg_ttft']:.3f}s, RT={result['avg_rt']:.3f}s")

        # 🔥 新增：整体成功率断言
        total_requests = sum(r['requests'] for r in stage_results)
        expected_min_requests = sum(s['concurrent'] * s['duration'] for s in stages) * 0.8  # 预期80%成功率
        assert total_requests >= expected_min_requests, f"总请求数过低: {total_requests} < {expected_min_requests}"

        print(f"✅ 阶梯压力测试通过: 总请求数={total_requests}")

    def test_20_边界值测试(self, tts_url):
        """指标20：边界值测试"""
        print("\n[边界值测试]")

        test_cases = [
            {"text": "A", "desc": "1个英文字符"},
            {"text": "测", "desc": "1个中文字符"},
            {"text": "测试" * 250, "desc": "500字符"},
            {"text": "测试" * 2500, "desc": "5000字符"},
            {"text": "a" * 1000, "desc": "1000英文字符"},
            {"text": "你好" + " " * 10 + "世界", "desc": "包含空格"},
            {"text": "\n\n\n", "desc": "纯换行符"},
        ]

        success_count = 0
        for case in test_cases:
            payload = {
                "model": "instruct2",
                "input": case["text"],
                "tts_params": {
                    "instruct_text": "You are a helpful assistant. 用自然的语气说<|endofprompt|>",
                    "prompt_audio": "kehu_female_b",
                    "zero_shot_spk_id": "kehu_female_b",
                    "speed": 1.0,
                    "stream": True
                }
            }

            try:
                res = tts_request_with_sync(tts_url, payload, enable_playback=False)
                if res["size"] > 0:
                    success_count += 1
            except:
                pass

        print(f"成功率: {success_count}/{len(test_cases)}")
        assert success_count >= len(test_cases) * 0.7

    def test_21_语速精细测试(self, tts_url, perf_stats):
        """指标21：语速精细测试"""
        print("\n[语速测试]")

        speeds = [0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.4, 1.5]
        test_text = "测试不同语速下的语音合成效果。"

        results = []
        for speed in speeds:
            payload = {
                "model": "instruct2",
                "input": test_text,
                "tts_params": {
                    "instruct_text": "You are a helpful assistant. 用自然的语气说<|endofprompt|>",
                    "prompt_audio": "kehu_female_b",
                    "zero_shot_spk_id": "kehu_female_b",
                    "speed": speed,
                    "stream": True
                }
            }

            try:
                res = tts_request_with_sync(tts_url, payload, enable_playback=False)
                results.append({"speed": speed, "success": True})
            except:
                results.append({"speed": speed, "success": False})

        success_count = sum(1 for r in results if r["success"])
        print(f"成功: {success_count}/{len(speeds)}")

        available_speeds = [r["speed"] for r in results if r["success"]]
        if available_speeds:
            print(f"语速范围: {min(available_speeds)}~{max(available_speeds)}")

    def test_22_情感合成测试(self, tts_url):
        """指标22：情感合成测试"""
        print("\n[情感测试]")

        test_cases = [
            {"text": "我很高兴今天天气真好！", "emotion": "happy"},
            {"text": "听到这个消息我很难过。", "emotion": "sad"},
            {"text": "这真让人生气！", "emotion": "angry"},
            {"text": "哇，太令人惊讶了！", "emotion": "surprise"},
            {"text": "这件事很重要。", "emotion": "serious"},
        ]

        success, skipped = 0, 0
        for case in test_cases:
            payload_with_emotion = {
                "model": "instruct2",
                "input": case["text"],
                "tts_params": {
                    "instruct_text": f"You are a helpful assistant. 用{case['emotion']}的情感说<|endofprompt|>",
                    "prompt_audio": "kehu_female_b",
                    "zero_shot_spk_id": "kehu_female_b",
                    "speed": 1.0,
                    "emotion": case["emotion"],
                    "stream": True
                }
            }

            try:
                res = tts_request_with_sync(tts_url, payload_with_emotion, enable_playback=False)
                if res["size"] > 0:
                    success += 1
            except Exception as e:
                if "emotion" in str(e).lower() or "不支持" in str(e):
                    skipped += 1

        print(f"结果: 成功{success}, 跳过{skipped}")

    def test_23_音频格式验证(self, tts_url):
        """指标23：音频格式验证测试"""
        print("\n[音频格式验证]")

        test_text = "测试音频格式验证。"
        payload = {
            "model": "instruct2",
            "input": test_text,
            "tts_params": {
                "instruct_text": "You are a helpful assistant. 用自然的语气说<|endofprompt|>",
                "prompt_audio": "kehu_female_b",
                "zero_shot_spk_id": "kehu_female_b",
                "speed": 1.0,
                "stream": True
            }
        }

        res = tts_request_with_sync(tts_url, payload, enable_playback=False)
        with wave.open(res["path"], 'rb') as wav:
            params = wav.getparams()
            duration = params.nframes / float(params.framerate) if params.framerate > 0 else 0

        print(f"音频: {os.path.basename(res['path'])}")
        print(f"时长: {duration:.2f}s, 采样率: {params.framerate}Hz, 声道: {params.nchannels}")

        assert params.nchannels in [1, 2]
        assert params.sampwidth in [1, 2, 3, 4]
        assert params.framerate in [8000, 16000, 24000, 44100, 48000]
        assert duration > 0.1
        print("通过")

    def test_24_端到端流程测试(self, tts_url, asr_request, crr_calculator):
        """指标24：TTS-ASR端到端流程测试"""
        print("\n[端到端测试]")

        test_cases = [
            {"text": "你好，这是端到端测试。"},
            {"text": "测试数字：1234567890"},
            {"text": "混合内容：Hello 123 测试！"},
            {"text": "这是一个稍长的句子，用于测试端到端的准确率。"},
        ]

        accuracies = []
        for case in test_cases:
            payload = {
                "model": "instruct2",
                "input": case["text"],
                "tts_params": {
                    "instruct_text": "You are a helpful assistant. 用清晰的语气说<|endofprompt|>",
                    "prompt_audio": "kehu_female_b",
                    "zero_shot_spk_id": "kehu_female_b",
                    "speed": 1.0,
                    "stream": True
                }
            }

            try:
                tts_result = tts_request_with_sync(tts_url, payload, enable_playback=False)
                asr_text = asr_request(tts_result["path"])
                if asr_text:
                    accuracy = crr_calculator(case["text"], asr_text)
                    accuracies.append(accuracy)
            except:
                pass

        if accuracies:
            avg_accuracy = statistics.mean(accuracies)
            print(f"平均准确率: {avg_accuracy:.4f} ({len(accuracies)}/{len(test_cases)}样本)")
            assert avg_accuracy >= 0.70
        else:
            print("无有效结果")
            pytest.skip("ASR接口无返回")