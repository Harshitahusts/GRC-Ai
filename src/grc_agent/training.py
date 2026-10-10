"""DPDP awareness training for a client's employees: the course, and the rules for passing.

Each lesson is a short read (and, once someone adds one, a video). A lesson can't be
skipped: the quiz opens only after the whole video has been watched (or the lesson read
for its full time), and the server counts the time itself, so dragging the video forward
doesn't help. Videos can play at 0.5x to 2x. A score of 70% or more passes; below that,
the lesson has to be watched again before another try.

The content is plain-English training, not legal advice; it follows the DPDP Act 2023 and
the DPDP Rules 2025 as summarised in grc_agent/obligations.py.
"""

from __future__ import annotations

from dataclasses import dataclass

PASS_MARK = 70  # percent
SPEEDS = (0.5, 1.0, 1.5, 2.0)
MAX_SPEED = max(SPEEDS)


@dataclass(frozen=True)
class Question:
    text: str
    options: tuple[str, ...]
    answer: int  # index into options


@dataclass(frozen=True)
class Lesson:
    id: str
    title: str
    minutes: int  # reading time when there is no video
    summary: str
    body: tuple[str, ...]  # paragraphs
    quiz: tuple[Question, ...]

    @property
    def read_seconds(self) -> int:
        return self.minutes * 60


COURSE: tuple[Lesson, ...] = (
    Lesson(
        "L1",
        "The DPDP Act and why it matters to you",
        3,
        "What the law is, who it protects, and what happens when it is broken.",
        (
            "The Digital Personal Data Protection Act 2023 (the DPDP Act) is India's law on "
            "personal data. The DPDP Rules 2025 fill in the detail. Most duties apply from "
            "13 May 2027.",
            "It protects Data Principals: the people the data is about, such as our "
            "customers, our colleagues and job applicants. Our organisation is a Data "
            "Fiduciary: we decide why and how their data is used, so we are responsible "
            "for it, including what our vendors (Data Processors) do with it for us.",
            "The Data Protection Board of India can fine an organisation up to ₹250 crore "
            "for failing to protect personal data, and up to ₹200 crore for not reporting a "
            "breach. Fines are per breach.",
            "You handle personal data every day: emails, spreadsheets, CRM records, "
            "screenshots, call recordings. Each of us has a part in keeping it safe.",
        ),
        (
            Question(
                "Who is a 'Data Principal'?",
                (
                    "The company that collects the data",
                    "The person the personal data is about",
                    "The Data Protection Officer",
                    "The IT team",
                ),
                1,
            ),
            Question(
                "Who is responsible for data a vendor processes on our behalf?",
                (
                    "Only the vendor",
                    "Nobody",
                    "Our organisation, as Data Fiduciary",
                    "The customer",
                ),
                2,
            ),
            Question(
                "What is the maximum penalty for failing to take reasonable security safeguards?",
                ("₹5 lakh", "₹10 crore", "₹50 crore", "₹250 crore"),
                3,
            ),
            Question(
                "Which of these is personal data?",
                (
                    "A customer's phone number in a spreadsheet",
                    "Our office's annual electricity bill",
                    "A list of product prices",
                    "The company's logo",
                ),
                0,
            ),
            Question(
                "From when do most DPDP duties apply?",
                ("Already since 2023", "13 May 2027", "Only after a breach", "2030"),
                1,
            ),
        ),
    ),
    Lesson(
        "L2",
        "Notice and consent",
        3,
        "Tell people what you collect and why; collect only with a valid reason.",
        (
            "Before asking for consent we must give a clear notice: what personal data we "
            "collect, for what purpose, how to withdraw consent, how to exercise rights and "
            "how to complain to the Board.",
            "Consent must be free, specific, informed, unconditional and unambiguous, given "
            "by a clear action such as ticking an unticked box. Pre-ticked boxes and "
            "'by using this site you agree' don't count.",
            "Collect only what the purpose needs. If a form asks for date of birth but the "
            "service doesn't need it, don't collect it.",
            "Withdrawing consent must be as easy as giving it. When someone withdraws, stop "
            "using their data for that purpose and tell our vendors to stop too.",
            "Some uses don't need consent (legitimate uses in Section 7), for example data "
            "a person gave voluntarily for a specific purpose, or employment purposes. If "
            "unsure, ask the privacy team.",
        ),
        (
            Question(
                "Which is valid consent?",
                (
                    "A pre-ticked box",
                    "Silence after an email",
                    "The person ticks an unticked box after reading the notice",
                    "A line in the footer saying 'by using this site you agree'",
                ),
                2,
            ),
            Question(
                "What must a notice include?",
                (
                    "The data collected and why, and how to withdraw and complain",
                    "Only the company's address",
                    "The price of the product",
                    "Nothing, if the form is short",
                ),
                0,
            ),
            Question(
                "A marketing form asks for Aadhaar numbers that aren't needed. What should happen?",
                (
                    "Keep it, more data is better",
                    "Remove the field: collect only what the purpose needs",
                    "Make it optional but pre-filled",
                    "Store it encrypted and keep asking",
                ),
                1,
            ),
            Question(
                "How easy must it be to withdraw consent?",
                (
                    "Harder than giving it",
                    "As easy as giving it",
                    "Only by posted letter",
                    "Withdrawal isn't allowed",
                ),
                1,
            ),
            Question(
                "After a customer withdraws consent, who must stop processing for that purpose?",
                (
                    "Only the marketing team",
                    "Nobody until the next audit",
                    "Us and the vendors processing it for us",
                    "Only the vendors",
                ),
                2,
            ),
        ),
    ),
    Lesson(
        "L3",
        "People's rights and handling requests",
        3,
        "Access, correction, erasure, grievances and nomination, and the clock.",
        (
            "People can ask for a summary of their personal data and who it was shared "
            "with; ask us to correct, complete, update or erase it; raise a grievance; and "
            "nominate someone to act for them on death or incapacity.",
            "We publish how to make a request and answer within the time we publish, never "
            "more than 90 days.",
            "If a request reaches you by email, phone or social media, don't ignore it and "
            "don't reply on your own: log it and pass it to the privacy team the same day.",
            "Check the person's identity before sharing or changing data, so we don't give "
            "one person's data to someone else.",
        ),
        (
            Question(
                "A customer emails you asking what data we hold on them. What do you do?",
                (
                    "Ignore it",
                    "Reply with an export of the CRM yourself",
                    "Log it and pass it to the privacy team the same day",
                    "Ask them to write to the Board",
                ),
                2,
            ),
            Question(
                "The longest we may take to respond to a rights request is:",
                ("7 days", "30 days", "90 days", "1 year"),
                2,
            ),
            Question(
                "Before sending someone their data we must:",
                (
                    "Verify their identity",
                    "Charge a fee",
                    "Get their manager's approval",
                    "Publish it on our website",
                ),
                0,
            ),
            Question(
                "Which is NOT a right under the DPDP Act?",
                (
                    "Correction of data",
                    "Nominating someone",
                    "Erasure of data",
                    "Being paid for their data",
                ),
                3,
            ),
            Question(
                "A grievance is:",
                (
                    "A complaint about how we handled their data",
                    "A request for a discount",
                    "A job application",
                    "An internal audit",
                ),
                0,
            ),
        ),
    ),
    Lesson(
        "L4",
        "Keeping data safe and spotting a breach",
        4,
        "Everyday safeguards, and what to do in the first hour of a breach.",
        (
            "Reasonable safeguards include encryption, limiting access to people who need "
            "it, logging access, and backups. Most breaches start with simple mistakes: a "
            "wrong recipient, a public link, a lost laptop, a phishing click.",
            "Lock your screen, use the password manager and MFA, never share passwords, "
            "and don't move customer data to personal email, WhatsApp or USB drives.",
            "A personal data breach is any unauthorised processing, accidental disclosure, "
            "loss of access, alteration or destruction of personal data.",
            "If you suspect one, tell the privacy or security team immediately, even if you "
            "are not sure. The organisation must tell affected people and the Board without "
            "delay and send the Board a detailed report within 72 hours. Hiding it makes it "
            "worse.",
        ),
        (
            Question(
                "You emailed a customer list to the wrong person. What now?",
                (
                    "Hope they don't open it",
                    "Report it to the privacy/security team immediately",
                    "Wait a week and see",
                    "Delete your sent mail",
                ),
                1,
            ),
            Question(
                "Within how long must the detailed breach report reach the Board?",
                ("24 hours", "72 hours", "30 days", "No deadline"),
                1,
            ),
            Question(
                "Which is a personal data breach?",
                (
                    "A stolen laptop with customer files",
                    "A slow website",
                    "A power cut with no data affected",
                    "A new product launch",
                ),
                0,
            ),
            Question(
                "Where may you store customer data?",
                (
                    "Personal Gmail",
                    "A USB stick at home",
                    "Approved company systems only",
                    "WhatsApp groups",
                ),
                2,
            ),
            Question(
                "Who must be told about a breach?",
                (
                    "Nobody",
                    "Only the CEO",
                    "Affected people and the Data Protection Board",
                    "Only the police",
                ),
                2,
            ),
        ),
    ),
    Lesson(
        "L5",
        "Children, vendors and sending data abroad",
        3,
        "Extra care for children's data; contracts for vendors; transfer limits.",
        (
            "A child is anyone under 18. Processing a child's data needs verifiable consent "
            "of a parent or lawful guardian. No tracking, behavioural monitoring or targeted "
            "advertising aimed at children.",
            "Every vendor that handles personal data for us needs a contract (a data "
            "processing agreement) before they get any data, and must protect it as we do.",
            "Personal data may go abroad except to countries the Government restricts. Keep "
            "track of where our tools store data, and check with the privacy team before "
            "adding a new tool or vendor.",
        ),
        (
            Question(
                "Under the DPDP Act a child is anyone under:",
                ("13", "16", "18", "21"),
                2,
            ),
            Question(
                "Before a new SaaS vendor gets customer data we need:",
                (
                    "A signed contract covering data processing",
                    "Nothing, if it's popular",
                    "Only a free trial",
                    "The vendor's logo",
                ),
                0,
            ),
            Question(
                "Is targeted advertising at children allowed?",
                ("Yes, always", "Only on weekends", "No", "Only with an ad agency"),
                2,
            ),
            Question(
                "Before signing up for a new tool that will hold customer data you should:",
                (
                    "Just start using it",
                    "Check with the privacy team",
                    "Use your personal card",
                    "Share the login with the team",
                ),
                1,
            ),
            Question(
                "Processing a child's data needs:",
                (
                    "The child's own consent",
                    "Verifiable consent of a parent or guardian",
                    "No consent",
                    "A school certificate",
                ),
                1,
            ),
        ),
    ),
    Lesson(
        "L6",
        "Keeping only what we need, and erasing it",
        3,
        "Retention, erasure and the 48-hour notice.",
        (
            "Keep personal data only while the purpose needs it or the law requires it. "
            "When the purpose is served or consent is withdrawn, erase it, and have our "
            "vendors erase it too.",
            "For the platforms the Rules list, data must be erased after three years "
            "without contact, and the person must be told at least 48 hours before, so they "
            "can log in to keep their account.",
            "Logs of processing are kept for at least one year. Old exports, spreadsheets "
            "and test copies are the most common forgotten data: delete them when done.",
        ),
        (
            Question(
                "When should personal data be erased?",
                (
                    "Never",
                    "When the purpose is served or consent is withdrawn, unless a law says keep it",
                    "Only after a breach",
                    "Every Friday",
                ),
                1,
            ),
            Question(
                "Where the Rules' retention periods apply, how much notice before erasure?",
                ("None", "1 hour", "At least 48 hours", "1 year"),
                2,
            ),
            Question(
                "You made a CSV export of customers for a one-off analysis. Afterwards you:",
                (
                    "Keep it on your desktop just in case",
                    "Email it to yourself",
                    "Delete it when the analysis is done",
                    "Upload it to a shared drive forever",
                ),
                2,
            ),
            Question(
                "Processing logs must be kept for at least:",
                ("1 week", "1 month", "1 year", "10 years"),
                2,
            ),
            Question(
                "When we erase data, our vendors should:",
                (
                    "Keep their copy",
                    "Erase their copy too",
                    "Sell it",
                    "Archive it publicly",
                ),
                1,
            ),
        ),
    ),
)

BY_ID = {lesson.id: lesson for lesson in COURSE}


def score(lesson: Lesson, answers: dict[str, str]) -> tuple[int, list[int]]:
    """(percent, indexes of questions answered wrongly). Answers map 'q0'.. to option index."""
    wrong = [i for i, q in enumerate(lesson.quiz) if answers.get(f"q{i}", "") != str(q.answer)]
    right = len(lesson.quiz) - len(wrong)
    return round(100 * right / len(lesson.quiz)), wrong


def passed(percent: int) -> bool:
    return percent >= PASS_MARK


def advance(watched: float, position: float, elapsed: float, duration: float) -> float:
    """How far someone has really watched, given where the player says it is now.

    Progress can't move faster than the top playback speed allows for the real time that
    passed (plus a little slack for timers), so seeking forward or faking the position
    doesn't count. Going back is fine; it just doesn't add anything.
    """
    allowed = max(0.0, elapsed) * MAX_SPEED + 2.0
    reached = min(position, watched + allowed, duration)
    return max(watched, reached)


def watch_complete(watched: float, duration: float) -> bool:
    return duration > 0 and watched >= duration - 2
