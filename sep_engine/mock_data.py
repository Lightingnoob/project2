"""
Mock data: Chabot College -> UC Berkeley -> Computer Science (the frontend's "Golden Pathway").

Course codes, difficulty scores, term offerings and meeting times are
illustrative, not real articulation data. Replace with rows from the real
database by producing the same models (or an SEPRequest JSON document).
"""

from __future__ import annotations

from .models import (Course, GroupCategory, MeetingTime, RequirementGroup, SEPRequest,
                     StudentProfile, Term, TransferAgreement)

F, S, SU = Term.FALL, Term.SPRING, Term.SUMMER
ANY = frozenset({F, S, SU})
REGULAR = frozenset({F, S})          # not offered in Summer (typical for labs / upper-sequence STEM)
FALL_ONLY = frozenset({F})
SPRING_ONLY = frozenset({S})


def _c(code: str, title: str, units: float, difficulty: float, prereqs=(), coreqs=(),
       terms=ANY, meetings=()) -> Course:
    return Course(code=code, title=title, units=units, difficulty=difficulty,
                  prereqs=list(prereqs), coreqs=list(coreqs), terms_offered=terms,
                  meetings=[MeetingTime(days=d, start=s, end=e) for d, s, e in meetings])


MOCK_CATALOG: list[Course] = [
    # --- Math / CS major prep ---
    _c("MATH 1", "Calculus I", 5, 3.8, meetings=[("MTWR", "08:00", "09:05")]),
    _c("MATH 2", "Calculus II", 5, 4.1, ["MATH 1"], terms=REGULAR, meetings=[("MTWR", "09:30", "10:35")]),
    _c("MATH 6", "Linear Algebra", 3, 3.5, ["MATH 2"], terms=FALL_ONLY, meetings=[("MW", "11:00", "12:15")]),
    _c("CS 1", "Intro to Programming", 4, 2.9, meetings=[("TR", "13:00", "14:50")]),
    _c("CS 2", "Data Structures", 4, 3.6, ["CS 1"], terms=REGULAR, meetings=[("MW", "13:00", "14:50")]),
    # CS 7 overlaps MATH 2 on Tue/Thu, so the two can never share a term
    _c("CS 7", "Discrete Structures", 4, 3.7, ["CS 1", "MATH 1"], terms=REGULAR, meetings=[("TR", "09:00", "10:50")]),
    _c("CS 20", "Computer Architecture", 4, 3.9, ["CS 2"], terms=SPRING_ONLY, meetings=[("TR", "15:00", "16:50")]),

    # --- Major science options (lab courses: never offered in Summer) ---
    _c("PHYS 4A", "Physics: Mechanics (with lab)", 4, 4.4, ["MATH 1"], terms=REGULAR,
       meetings=[("TR", "11:00", "12:50"), ("F", "09:00", "11:50")]),
    _c("CHEM 1A", "General Chemistry I (with lab)", 5, 3.9, terms=REGULAR,
       meetings=[("MW", "15:00", "16:50"), ("F", "13:00", "15:50")]),

    # --- GE: English, critical thinking, oral communication ---
    _c("ENGL 1A", "College Composition", 3, 2.6),
    _c("ENGL 7", "Critical Thinking & Writing", 3, 3.0, ["ENGL 1A"]),
    _c("PHIL 2", "Introduction to Logic", 3, 3.2),
    _c("SPCH 1", "Public Speaking", 3, 2.4),
    _c("SPCH 10", "Interpersonal Communication", 3, 1.9),
    _c("STAT 1", "Introduction to Statistics", 3, 2.8),

    # --- GE: Arts & Humanities ---
    _c("ART 2", "Art History: Renaissance to Modern", 3, 2.2),
    _c("MUS 10", "Music Appreciation", 3, 1.6),
    _c("MUS 12", "Music Fundamentals", 2, 3.0),
    _c("PHIL 1", "Introduction to Philosophy", 3, 2.9),
    _c("HIST 7", "History of the United States", 3, 2.6),
    _c("ENGL 45", "Mythology", 3, 2.1),
    _c("HUMN 2", "World Cultures & the Arts", 3, 2.4),

    # --- GE: Social & Behavioral Sciences ---
    _c("PSYC 1", "General Psychology", 3, 2.0),
    _c("SOCI 1", "Introduction to Sociology", 3, 1.8),
    _c("ECON 1", "Principles of Macroeconomics", 3, 3.3),
    _c("ECON 2", "Principles of Microeconomics", 3, 3.4),
    _c("ANTH 3", "Cultural Anthropology", 3, 2.2),
    _c("POSC 1", "American Government", 3, 2.6),
    _c("GEOG 2", "Cultural Geography", 3, 2.3),
    _c("COMM 20", "Mass Media & Society", 3, 2.1),
    _c("CDEV 50", "Child Development", 3, 1.5, ["PSYC 1"]),

    # --- GE: Sciences (lecture + co-requisite labs) ---
    _c("ASTR 10", "Descriptive Astronomy", 3, 2.1),
    _c("ASTR 10L", "Astronomy Lab", 1, 1.5, coreqs=["ASTR 10"]),
    _c("GEOL 1", "Physical Geology", 3, 2.4),
    _c("BIOL 10", "Introduction to Biology", 3, 2.3, meetings=[("MW", "09:00", "10:15")]),
    _c("BIOL 10L", "Biology Lab", 1, 1.8, coreqs=["BIOL 10"], meetings=[("F", "12:00", "14:50")]),
    _c("ANTH 1", "Physical Anthropology", 3, 2.5),

    # --- GE: Ethnic Studies ---
    _c("ETHS 1", "Introduction to Ethnic Studies", 3, 2.4),
    _c("ETHS 10", "Race & Ethnicity in America", 3, 2.0),
]

_REQUIRED = ["MATH 1", "MATH 2", "MATH 6", "CS 1", "CS 2", "CS 7", "CS 20"]
_MAJOR_SCIENCE = RequirementGroup(id="major-sci", name="Major prep: one lab science", category=GroupCategory.MAJOR,
                                  choose=1, options=["PHYS 4A", "CHEM 1A"])
_PATHWAY = dict(college="Chabot College", university="UC Berkeley", major="Computer Science",
                degree="Computer Science, B.A.", required_courses=_REQUIRED)

MOCK_AGREEMENT = TransferAgreement(
    **_PATHWAY,
    ge_pattern="CAL-GETC",
    requirement_groups=[
        _MAJOR_SCIENCE,
        RequirementGroup(id="1A", name="Area 1A: English Composition", category=GroupCategory.GE,
                         options=["ENGL 1A"]),
        RequirementGroup(id="1B", name="Area 1B: Critical Thinking", category=GroupCategory.GE,
                         options=["ENGL 7", "PHIL 2"]),
        RequirementGroup(id="1C", name="Area 1C: Oral Communication", category=GroupCategory.GE,
                         options=["SPCH 1", "SPCH 10"]),
        RequirementGroup(id="2", name="Area 2: Mathematical Concepts", category=GroupCategory.GE,
                         options=["MATH 1", "STAT 1"]),
        RequirementGroup(id="3", name="Area 3: Arts & Humanities", category=GroupCategory.GE, choose=2,
                         options=["ART 2", "MUS 10", "MUS 12", "PHIL 1", "HIST 7", "ENGL 45", "HUMN 2"]),
        RequirementGroup(id="4", name="Area 4: Social & Behavioral Sciences", category=GroupCategory.GE, choose=2,
                         options=["PSYC 1", "SOCI 1", "ECON 1", "ECON 2", "ANTH 3", "POSC 1",
                                  "GEOG 2", "COMM 20", "CDEV 50", "HIST 7"]),
        RequirementGroup(id="5A", name="Area 5A: Physical Science", category=GroupCategory.GE,
                         options=["ASTR 10", "GEOL 1", "PHYS 4A", "CHEM 1A"]),
        RequirementGroup(id="5B", name="Area 5B: Biological Science", category=GroupCategory.GE,
                         options=["BIOL 10", "ANTH 1"]),
        RequirementGroup(id="5C", name="Area 5C: Laboratory", category=GroupCategory.GE,
                         options=["BIOL 10L", "ASTR 10L", "PHYS 4A", "CHEM 1A"]),
        RequirementGroup(id="6", name="Area 6: Ethnic Studies", category=GroupCategory.GE,
                         options=["ETHS 1", "ETHS 10"]),
    ],
)

MOCK_AGREEMENT_7COURSE = TransferAgreement(
    **_PATHWAY,
    ge_pattern="7-Course Pattern",
    requirement_groups=[
        _MAJOR_SCIENCE,
        RequirementGroup(id="R1A", name="Reading & Composition A", category=GroupCategory.GE,
                         options=["ENGL 1A"]),
        RequirementGroup(id="R1B", name="Reading & Composition B", category=GroupCategory.GE,
                         options=["ENGL 7"]),
        RequirementGroup(id="AL", name="Arts & Literature", category=GroupCategory.GE,
                         options=["ART 2", "MUS 10", "ENGL 45"]),
        RequirementGroup(id="BS", name="Biological Science", category=GroupCategory.GE,
                         options=["BIOL 10", "ANTH 1"]),
        RequirementGroup(id="HS", name="Historical Studies", category=GroupCategory.GE,
                         options=["HIST 7"]),
        RequirementGroup(id="IS", name="International Studies", category=GroupCategory.GE,
                         options=["ANTH 3", "GEOG 2"]),
        RequirementGroup(id="PV", name="Philosophy & Values", category=GroupCategory.GE,
                         options=["PHIL 1", "PHIL 2"]),
        RequirementGroup(id="PS", name="Physical Science", category=GroupCategory.GE,
                         options=["ASTR 10", "GEOL 1", "PHYS 4A", "CHEM 1A"]),
        RequirementGroup(id="SBS", name="Social & Behavioral Sciences", category=GroupCategory.GE,
                         options=["PSYC 1", "SOCI 1", "ECON 1", "POSC 1"]),
    ],
)

MOCK_AGREEMENTS = [MOCK_AGREEMENT, MOCK_AGREEMENT_7COURSE]

MOCK_PROFILE = StudentProfile(completed_courses=["CS 1", "ENGL 1A"], start_term=Term.FALL, start_year=2026)


def mock_request() -> SEPRequest:
    return SEPRequest(catalog=MOCK_CATALOG, agreement=MOCK_AGREEMENT, profile=MOCK_PROFILE)
