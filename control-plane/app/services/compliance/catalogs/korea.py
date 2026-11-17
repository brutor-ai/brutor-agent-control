# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Korea — AI Framework Act (`regulation`)

인공지능 발전과 신뢰 기반 조성 등에 관한 기본법 (Framework Act on the
Development of Artificial Intelligence and the Creation of a Foundation for
Trust), Act No. 20676 of 21 January 2025, in force 22 January 2026 (one
year after promulgation, Addenda Art 1), and its Enforcement Decree
(Presidential Decree No. 36053 of 21 January 2026, in force 22 January
2026): advance notice of high-impact and generative AI, labelling of
generative outputs, the advance high-impact review, the safety and
reliability measures for high-impact AI, and the domestic agent of a
foreign operator.

Deliberately not catalogued: Art 35(1) fundamental-rights impact
assessment (a duty to endeavour, 노력하여야, not a mandatory duty) and Art
32 safety duties (they bind only AI systems trained with at least 10^26
FLOPs that also meet the other Decree Art 24 tests — frontier-model
duties, not this product's users'). Art 31(1) advance notice is split in
two entries (generative / high-impact) because applicability conditions
combine with AND; a system declared neither is out of scope of both.

Checked on 2026-10-16 against the official Korean text on the National Law
Information Center (law.go.kr): the Act as amended by Act No. 21311
(20 January 2026, consolidated version in force 21 July 2026) and the
Decree as amended by Presidential Decree No. 36580 (18 August 2026,
consolidated version in force 20 August 2026). The amendments did not
touch Arts 31, 33, 34 or 36 of the Act or Arts 23, 27 or 29 of the Decree. English
excerpts are from KLRI's official translation (elaw.klri.re.kr) of the
original Act No. 20676; the Korean text is authoritative. No English
translation of the Decree was used. Not legal advice.
"""

from __future__ import annotations

from datetime import date
from types import MappingProxyType

from app.services.compliance.catalogs import common as c
from app.services.compliance.catalogs.base import (
    INSTRUMENT_FINAL,
    KIND_REGULATION,
    CatalogVersion,
    Framework,
    Obligation,
    Source,
)

FRAMEWORK_ID = "korea"
CATALOG_VERSION = "2026-09"

APPLIES = date(2026, 1, 22)

CHECKED = date(2026, 10, 16)

_ACT = "인공지능 발전과 신뢰 기반 조성 등에 관한 기본법 (Act No. 20676, as amended by Act No. 21311)"
_ACT_EN = (
    "Framework Act on the Development of Artificial Intelligence and the Creation of a Foundation "
    "for Trust (Act No. 20676), KLRI official English translation"
)
_DECREE = (
    "인공지능 발전과 신뢰 기반 조성 등에 관한 기본법 시행령 (Presidential Decree No. 36053, "
    "as amended by Presidential Decree No. 36580)"
)
_ACT_URL = "https://www.law.go.kr/법령/인공지능발전과신뢰기반조성등에관한기본법"
_DECREE_URL = "https://www.law.go.kr/법령/인공지능발전과신뢰기반조성등에관한기본법시행령"
_EN_URL = "https://elaw.klri.re.kr/eng_mobile/viewer.do?hseq=71019&type=new&key="

#: Retention of the documents behind the high-impact measures (Decree Art 27(2)).
HIGH_IMPACT_DOCUMENT_RETENTION_DAYS = 5 * 365

#: Art 31(1) and Decree Art 23(1): one duty, cited by both halves of its scope.
_ART31_1_SOURCES = (
    Source(
        instrument=_ACT,
        provision="제31조제1항 (Art 31(1))",
        url=f"{_ACT_URL}/제31조",
        checked_on=CHECKED,
        excerpt=(
            "인공지능사업자는 고영향 인공지능이나 생성형 인공지능을 이용한 제품 또는 서비스를 "
            "제공하려는 경우 제품 또는 서비스가 해당 인공지능에 기반하여 운용된다는 사실을 "
            "이용자에게 사전에 고지하여야 한다."
        ),
    ),
    Source(
        instrument=_ACT_EN,
        provision="Art 31(1)",
        url=_EN_URL,
        checked_on=CHECKED,
        excerpt=(
            "An artificial intelligence business operator that intends to provide a product or "
            "service using high-impact artificial intelligence or generative artificial "
            "intelligence shall notify the user in advance that the product or service is "
            "operated based on the relevant artificial intelligence."
        ),
    ),
    Source(
        instrument=_DECREE,
        provision="제23조제1항 (Decree Art 23(1))",
        url=f"{_DECREE_URL}/제23조",
        checked_on=CHECKED,
        excerpt=(
            "다음 각 호의 어느 하나에 해당하는 방법으로 제품등이 해당 인공지능에 기반하여 "
            "운용된다는 사실을 이용자에게 사전에 고지해야 한다."
        ),
    ),
)

_E = c.CROSSWALK_PREFIX

_O = Obligation

OBLIGATIONS = [
    _O(
        key="advance-notice",
        title="Advance notice of generative AI",
        reference="Art 31(1); Enforcement Decree Art 23(1), (4)",
        applies_from=APPLIES,
        summary=(
            f"{_E} notifying users in advance that a product or service using generative AI is "
            "operated based on that AI — in the product, contract, terms or manual, on the user's "
            "screen or device, or posted where it is provided. The same duty covers high-impact AI "
            "(separate entry). The Decree allows exceptions where the AI use is obvious or the use "
            "is internal only."
        ),
        flags=frozenset({"generates_synthetic_content"}),
        evidence=[c.notice_before_first_turn(), c.notice_text()],
        sources=_ART31_1_SOURCES,
    ),
    _O(
        key="advance-notice-high-impact",
        title="Advance notice of high-impact AI",
        reference="Art 31(1); Enforcement Decree Art 23(1), (4)",
        applies_from=APPLIES,
        summary=(
            f"{_E} notifying users in advance that a product or service using high-impact AI is "
            "operated based on that AI, by the same methods and with the same exceptions as for "
            "generative AI (separate entry)."
        ),
        risk_tiers=frozenset({"high_impact"}),
        evidence=[c.notice_before_first_turn(), c.notice_text()],
        sources=_ART31_1_SOURCES,
    ),
    _O(
        key="generative-labelling",
        title="Labelling of generative AI outputs",
        reference="Art 31(2)–(3); Enforcement Decree Art 23(2)–(3)",
        applies_from=APPLIES,
        summary=(
            f"{_E} indicating that outputs were produced by generative AI (a machine-readable "
            "mark is allowed if the user is also told at least once by text or voice), and "
            "notifying or labelling realistic virtual sound, images or video so users clearly "
            "recognise them as AI-generated."
        ),
        flags=frozenset({"generates_synthetic_content"}),
        evidence=[c.synthetic_marking()],
        limit="Content-level marking is not provided by the gateway.",
        sources=(
            Source(
                instrument=_ACT,
                provision="제31조제2항 (Art 31(2))",
                url=f"{_ACT_URL}/제31조",
                checked_on=CHECKED,
                excerpt=(
                    "인공지능사업자는 생성형 인공지능 또는 이를 이용한 제품 또는 서비스를 제공하는 "
                    "경우 그 결과물이 생성형 인공지능에 의하여 생성되었다는 사실을 표시하여야 한다."
                ),
            ),
            Source(
                instrument=_ACT_EN,
                provision="Art 31(2)",
                url=_EN_URL,
                checked_on=CHECKED,
                excerpt=(
                    "An artificial intelligence business operator that provides generative artificial "
                    "intelligence or a product or service using it shall label that the output was "
                    "generated by generative artificial intelligence."
                ),
            ),
            Source(
                instrument=_DECREE,
                provision="제23조제2항제2호 (Decree Art 23(2)(ii))",
                url=f"{_DECREE_URL}/제23조",
                checked_on=CHECKED,
                excerpt=(
                    "기계가 판독할 수 있는 방법. 이 경우 그 결과물이 생성형 인공지능에 의하여 "
                    "생성되었다는 사실을 1회 이상 안내 문구ㆍ음성 등으로 제공해야 한다."
                ),
            ),
        ),
    ),
    _O(
        key="high-impact-measures",
        title="Safety and reliability measures for high-impact AI",
        reference="Art 34(1); Enforcement Decree Art 27",
        applies_from=APPLIES,
        summary=(
            f"{_E} the measures for high-impact AI: a risk management plan, an explanation plan, "
            "user protection, human management and oversight, and documents recording the "
            "measures — the main content posted, and the supporting documents kept for five years."
        ),
        risk_tiers=frozenset({"high_impact"}),
        evidence=[
            c.override_available(),
            c.oversight_exercised(),
            c.declared(
                "declared.high_impact_measures",
                "frameworks.korea.high_impact_measures_reference",
                "The high-impact AI measures are declared.",
                "Organisational duty; the measures' content is not evaluated.",
            ),
            c.retention_floor(
                HIGH_IMPACT_DOCUMENT_RETENTION_DAYS,
                "Documentation artefacts (reports, declarations, documentation exports) are kept for five years.",
                scope="documentation",
                limit=(
                    "Applies to documentation artefacts only, not to every log record; documents "
                    "kept outside the platform are not covered."
                ),
            ),
        ],
        sources=(
            Source(
                instrument=_ACT,
                provision="제34조제1항 (Art 34(1))",
                url=f"{_ACT_URL}/제34조",
                checked_on=CHECKED,
                excerpt=(
                    "인공지능사업자는 고영향 인공지능 또는 이를 이용한 제품ㆍ서비스를 제공하는 경우 "
                    "고영향 인공지능의 안전성ㆍ신뢰성을 확보하기 위하여 다음 각 호의 내용을 포함하는 "
                    "조치를 대통령령으로 정하는 바에 따라 이행하여야 한다."
                ),
            ),
            Source(
                instrument=_ACT_EN,
                provision="Art 34(1)(iv)–(v)",
                url=_EN_URL,
                checked_on=CHECKED,
                excerpt=(
                    "4. To assign human management and oversight of high-impact artificial "
                    "intelligence;5. To prepare and retain documents that can verify the content of "
                    "the measures taken to ensure the safety and trustworthiness;"
                ),
            ),
            Source(
                instrument=_DECREE,
                provision="제27조제2항 (Decree Art 27(2))",
                url=f"{_DECREE_URL}/제27조",
                checked_on=CHECKED,
                excerpt=(
                    "인공지능사업자는 법 제34조제1항 각 호의 조치를 이행하고 그 근거를 문서로 5년간 "
                    "보관(전자적 방법을 통한 보관을 포함한다)해야 한다."
                ),
            ),
        ),
    ),
    _O(
        key="high-impact-review",
        title="Advance review of whether the AI is high-impact",
        reference="Art 33(1)",
        applies_from=APPLIES,
        summary=(
            f"{_E} reviewing in advance, before providing an AI or a product or service using "
            "it, whether it is high-impact AI; the operator may ask the Minister of Science and "
            "ICT to confirm the determination."
        ),
        evidence=[
            c.declared(
                "declared.high_impact_review",
                "frameworks.korea.classification",
                "The system's high-impact determination is declared.",
                "The operator's own determination; its correctness is not evaluated, and any "
                "Ministry confirmation is held outside the platform.",
            ),
        ],
        sources=(
            Source(
                instrument=_ACT,
                provision="제33조제1항 (Art 33(1))",
                url=f"{_ACT_URL}/제33조",
                checked_on=CHECKED,
                excerpt=(
                    "인공지능사업자는 인공지능 또는 이를 이용한 제품ㆍ서비스를 제공하는 경우 그 "
                    "인공지능이 고영향 인공지능에 해당하는지에 대하여 사전에 검토하여야 하며"
                ),
            ),
            Source(
                instrument=_ACT_EN,
                provision="Art 33(1)",
                url=_EN_URL,
                checked_on=CHECKED,
                excerpt=(
                    "the artificial intelligence business operator shall review in advance whether "
                    "the artificial intelligence falls under the high-impact artificial intelligence"
                ),
            ),
        ),
    ),
    _O(
        key="domestic-agent",
        title="Designation of a domestic agent (foreign operators)",
        reference="Art 36(1)–(2); Enforcement Decree Art 29",
        applies_from=APPLIES,
        summary=(
            f"{_E} designating in writing, and reporting to the Minister of Science and ICT, a "
            "domestic agent with an address or office in Korea — required only of an operator "
            "with no address or business office in Korea that meets a Decree Art 29 threshold "
            "(prior-year revenue of KRW 1 trillion or more, AI-service revenue of KRW 10 billion "
            "or more, a daily average of 1 million or more domestic users over the prior three "
            "months, or a past fine for ignoring a correction order)."
        ),
        needs_review=True,
        review_note=(
            "Applies only to an operator without an address or office in Korea that meets an "
            "Enforcement Decree Art 29 threshold; the profile cannot express that, so the "
            "operator determines it."
        ),
        evidence=[
            c.declared(
                "declared.domestic_agent",
                "frameworks.korea.domestic_agent_reference",
                "The domestic agent designation (or that none is required) is declared.",
                "Whether the operator is in scope and the designation and report to the Ministry "
                "are organisational; the gateway records only the declaration.",
            ),
        ],
        limit=(
            "Scope turns on the operator's establishment, revenue and Korean user numbers, which "
            "the gateway does not hold; evidenced by declaration only."
        ),
        sources=(
            Source(
                instrument=_ACT,
                provision="제36조제1항 (Art 36(1))",
                url=f"{_ACT_URL}/제36조",
                checked_on=CHECKED,
                excerpt=(
                    "국내에 주소 또는 영업소가 없는 인공지능사업자로서 이용자 수, 매출액 등이 "
                    "대통령령으로 정하는 기준에 해당하는 자는 다음 각 호의 사항을 대리하는 자(이하 "
                    "“국내대리인”이라 한다)를 서면으로 지정하고, 이를 과학기술정보통신부장관에게 "
                    "신고하여야 한다."
                ),
            ),
            Source(
                instrument=_ACT_EN,
                provision="Art 36(1)",
                url=_EN_URL,
                checked_on=CHECKED,
                excerpt=(
                    "An artificial intelligence business operator that does not have an address or "
                    "business office in the country and meets the standards for the number of users, "
                    "sales, etc. as prescribed by Presidential Decree shall designate an agent"
                ),
            ),
            Source(
                instrument=_DECREE,
                provision="제29조제1항제3호 (Decree Art 29(1)(iii))",
                url=f"{_DECREE_URL}/제29조",
                checked_on=CHECKED,
                excerpt=(
                    "전년도 말 기준 해당 인공지능사업자의 인공지능제품 및 인공지능서비스에 대한 직전 "
                    "3개월간 국내 이용자 수가 1일 평균 100만명 이상인 인공지능사업자"
                ),
            ),
        ),
    ),
]

FRAMEWORK = Framework(
    id=FRAMEWORK_ID,
    title="Korea AI Framework Act",
    jurisdiction="KR",
    reference=(
        "Framework Act on the Development of Artificial Intelligence and the Creation of a "
        "Foundation for Trust (Act No. 20676) and its Enforcement Decree"
    ),
    kind=KIND_REGULATION,
    obligations=OBLIGATIONS,
    versions=(CatalogVersion(CATALOG_VERSION, effective_from=APPLIES, note="In force 22 January 2026"),),
    classifications=frozenset({"high_impact", "not_high_impact"}),
    classification_field="classification",
    deadline_rules=MappingProxyType({}),
    retention_floors=MappingProxyType({}),
    verified_against_instrument=True,
    instrument_status=INSTRUMENT_FINAL,
    verified_on=CHECKED,
)

__all__ = ["FRAMEWORK", "FRAMEWORK_ID"]
