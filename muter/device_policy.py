"""Shared physical-device selection. Never capture or monitor a virtual cable."""
import re


def virtual(name):
    n = name.casefold()
    return any(s in n for s in ("cable", "vb-audio", "steam streaming", "virtual", "voicemeeter", "立体声混音", "stereo mix", "loopback", "内部 aux"))


def family(name):
    matches = re.findall(r"\(([^()]*)\)", name)
    return matches[-1].casefold() if matches else name.casefold()


def choose(items, default=None, preferred=None, input_family=None, capture=False):
    candidates = [(i, d) for i, d in items if not virtual(d["name"])]
    if not candidates:
        raise RuntimeError("没有可用的实体麦克风。" if capture else "没有可用的耳机或扬声器。")
    def score(item):
        i, d = item; n = d["name"].casefold()
        if preferred and n == preferred.casefold(): return 1000
        if i == default: return 900
        value = 0
        if input_family and family(n) == input_family: value += 200
        if "hands-free" in n: value -= 90
        if any(s in n for s in ("digital", "s/pdif", "nvidia", "hdmi")): value -= 100
        if capture and any(s in n for s in ("microphone", "麦克风")): value += 50
        if "stereo" in n: value += 15
        return value
    return max(candidates, key=score)[0]


def names(items, index):
    return next((d["name"] for i, d in items if i == index), "")


def headphone(name):
    return not virtual(name) and any(s in name.casefold() for s in
        ("headphone", "headset", "earphone", "耳机", "耳麦", "airpods", "buds"))


def choose_monitor(items, input_name="", default=None):
    candidates = [(i, d) for i, d in items if headphone(d["name"])]
    if not candidates:
        raise RuntimeError("未检测到耳机。连接耳机后即可实时试听，避免扬声器回授啸叫。")
    # Classic Bluetooth HFP capture and A2DP stereo playback can conflict.
    # When using a headset's HFP mic, prefer its matching duplex HFP output.
    matching = [(i, d) for i, d in candidates if "hands-free" in input_name.casefold()
                and family(d["name"]) == family(input_name)]
    if matching:
        return matching[0][0]
    stereo = [(i, d) for i, d in candidates if "hands-free" not in d["name"].casefold()]
    return choose(stereo or candidates, default)
