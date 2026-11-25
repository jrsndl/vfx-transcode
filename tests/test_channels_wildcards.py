"""Wildcards in the channel preferences, and the layer keywords."""

from vfx_transcode.channels import layer_tokens, match_set, pick_channels
from vfx_transcode.names import NAME_KEYWORDS

INFO = [{"channelnames": ["A", "ActorHitProxyMask00.R", "ActorHitProxyMask00.G",
                          "ActorHitProxyMask00.B", "ActorZ.R", "beauty.R", "beauty.G", "beauty.B",
                          "depth.Z"]},
        {"channelnames": ["crypto.R", "crypto.G"], "attribs": {"name": "crypto"}}]


def test_wildcard_layer():
    pick = pick_channels(INFO, preferences=[["rgba.R", "rgba.G", "rgba.B"],
                                            ["Actor*.R", "Actor*.G", "Actor*.B"]])
    assert pick.channels == ["ActorHitProxyMask00.R", "ActorHitProxyMask00.G", "ActorHitProxyMask00.B"]
    assert pick.reason.startswith("preferred (Actor*.R")


def test_wildcard_needs_the_whole_set_in_one_layer():
    # ActorZ has only R: the first layer with R, G and B is used
    names = ["ActorZ.R", "ActorQ.R", "ActorQ.G", "ActorQ.B"]
    assert match_set(["Actor*.R", "Actor*.G", "Actor*.B"], names) == ["ActorQ.R", "ActorQ.G", "ActorQ.B"]
    assert match_set(["Actor*.R", "Actor*.G", "Actor*.B"], ["ActorZ.R"]) is None


def test_wildcard_first_match_wins_and_exact_sets_unchanged():
    assert match_set(["*.Z"], INFO[0]["channelnames"]) == ["depth.Z"]
    assert match_set(["beauty.R", "beauty.G", "beauty.B"], INFO[0]["channelnames"]) == \
        ["beauty.R", "beauty.G", "beauty.B"]
    assert match_set(["beauty.R", "beauty.X"], INFO[0]["channelnames"]) is None
    assert match_set(["actor*.R"], INFO[0]["channelnames"]) is None          # case counts
    # mixed layers: each pattern on its own
    assert match_set(["Actor*.R", "beauty.G"], INFO[0]["channelnames"]) == ["ActorHitProxyMask00.R", "beauty.G"]


def test_layer_tokens():
    tokens = layer_tokens(INFO)
    assert tokens["multi_layers_line"] == "(no layer), ActorHitProxyMask00, ActorZ, beauty, depth, crypto"
    assert tokens["multi_layers_column"].split("\n")[:2] == ["(no layer)", "ActorHitProxyMask00"]
    assert tokens["multi_layerschannels_line"].startswith("(no layer)[A], ActorHitProxyMask00[R,G,B], ActorZ[R]")
    assert tokens["multi_layerschannels_column"].split("\n")[-1] == "crypto[R,G]"
    assert layer_tokens(None) == {k: "" for k in tokens}
    assert set(tokens) <= set(NAME_KEYWORDS)
