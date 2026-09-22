#!/usr/bin/env python3
"""Author-time generator for this task's catalog and population SQL.

Writes two files next to itself:

  sql/002_reference.sql   card products, welcome offers, the knowledge base,
                          workflow and delivery profiles, the scenario clock,
                          the card-section read models, and the allocators
  sql/003_population.sql  customers, cards, ledger activity, restrictions,
                          referrals, sessions, notifications, and verification
                          records for a realistic estate

The rows the recorded conversation touches are not drawn from the random
population; they are declared in sql/004_scenario.sql, so regenerating with a
different RNG seed cannot move them. Everything here exists to make the lookups
non-trivial: customers who share a surname and a full name with the target,
trusted channels that are not enrolled, identity factors that do not match,
withdrawn products and expired offers that must be filtered out, restrictions
that confirming activity cannot lift, and sessions nobody ever opened.

This script never enters the container image; see environment/.dockerignore.

Usage:  python3 gen_seed.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
SQL = os.path.join(HERE, "sql")

RNG_SEED = 20260225
CONVERSATION_ID = "banking-transaction-dispute-session"
# The clock every mutation stamps. This conversation never reads it back, so no
# recorded result depends on the value; it is the recorded scenario time, and it
# is what keeps a run's timestamps reproducible.
SCENARIO_TIME = "2026-02-25T16:20:00-05:00"
CONVERSATION_STARTED_AT = "2026-02-25T16:18:00-05:00"
TIMEZONE = "America/New_York"

# The month the generated ledger sits in, and the offset its local times carry.
# A statement search is scoped by posting date, so an estate whose activity all
# fell in a different month would make the recorded search unique for the wrong
# reason. Kept in step with the scenario clock instead.
LEDGER_YEAR = 2026
LEDGER_MONTH = 2
LEDGER_LAST_DAY = 24
LEDGER_OFFSET = "-05:00"
TRAVEL_MONTH = 3
REFERRAL_MONTHS = (11, 12, 1, 2)

# Descriptor the recorded dispute is searched by. Generated marketplace
# descriptors take a random four-digit suffix, so this one is reserved: a second
# charge carrying it could satisfy the recorded search too.
RESERVED_DESCRIPTOR_SUFFIXES = {"8472"}

# Sections the recorded conversation reads more than once, and how a repeat read
# differs. This conversation never reads a card section at all, so every section
# serves one read model and reports its whole contents.
SECTION_POLICY = [
    ("status", "full"),
    ("available_credit", "full"),
    ("authorizations", "full"),
    ("declines", "full"),
    ("restrictions", "full"),
    ("travel_notices", "full"),
]

SECTION_VIEWS = [
    ("*", "status", 0, ["status", "reported_lost", "payment_status"]),
    ("*", "available_credit", 0, ["available_credit", "available_credit_currency"]),
    ("*", "authorizations", 0, ["transaction_id", "merchant", "merchant_location",
                                "amount", "currency", "status", "occurred_at"]),
    ("*", "declines", 0, ["transaction_id", "merchant", "merchant_location",
                          "amount", "currency", "status", "reason"]),
    ("*", "restrictions", 0, ["restriction_id", "status", "linked_transaction_ids"]),
    ("*", "travel_notices", 0, ["notice_id", "destinations", "return_date",
                                "authorization_guaranteed"]),
]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def q(value) -> str:
    """Render a Python value as a SQL literal."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        if not value:
            return "'{}'"
        inner = ",".join('"' + str(v).replace('"', '\\"') + '"' for v in value)
        return "'{" + inner + "}'"
    return "'" + str(value).replace("'", "''") + "'"


def jsonb(payload) -> str:
    return q(json.dumps(payload, separators=(",", ":"))) + "::jsonb"


def insert(table: str, columns: list, rows: list) -> str:
    if not rows:
        return ""
    head = f"INSERT INTO {table} ({', '.join(columns)}) VALUES\n"
    body = ",\n".join(
        "    (" + ", ".join(v if isinstance(v, Raw) else q(v) for v in row) + ")"
        for row in rows)
    return head + body + ";\n\n"


class Raw(str):
    """A pre-rendered SQL fragment, passed through instead of quoted."""


# ---------------------------------------------------------------------------
# card catalog
# ---------------------------------------------------------------------------

# product_id, product, family, category, annual_fee, fx_fee, lounge,
# incidental_credit, free_bag, airline_rules, active, rank
#
# Exactly two travel products are current, which is what makes the "current
# travel products" knowledge record return two rows. Summit Reserve Elite is
# withdrawn: it is a travel card with no foreign-transaction fee and lounge
# membership, so only the active flag keeps it out of the answer.
CARD_PRODUCTS = [
    ("summit-journey", "Summit Journey", "summit", "travel", "95.00",
     False, False, False, False, True, True, 10),
    ("summit-reserve", "Summit Reserve", "summit", "travel", "395.00",
     False, True, True, False, True, True, 20),
    ("summit-reserve-elite", "Summit Reserve Elite", "summit", "travel", "795.00",
     False, True, True, True, True, False, 30),
    ("everyday-cash", "Everyday Cash", "everyday", "cash_back", "0.00",
     True, False, False, False, False, True, 40),
    ("everyday-cash-plus", "Everyday Cash Plus", "everyday", "cash_back", "95.00",
     True, False, False, False, False, True, 50),
    ("everyday-cash-legacy", "Everyday Cash Legacy", "everyday", "cash_back", "39.00",
     True, False, False, False, False, False, 60),
    ("horizon-balance", "Horizon Balance", "horizon", "balance_transfer", "0.00",
     True, False, False, False, False, True, 70),
    ("horizon-balance-plus", "Horizon Balance Plus", "horizon", "balance_transfer",
     "49.00", True, False, False, False, False, True, 80),
    ("campus-start", "Campus Start", "campus", "student", "0.00",
     True, False, False, False, False, True, 90),
    ("foundation-secured", "Foundation Secured", "foundation", "secured", "0.00",
     True, False, False, False, False, True, 100),
    ("ledger-business", "Ledger Business", "ledger", "business", "95.00",
     False, False, False, False, True, True, 110),
    ("ledger-business-premier", "Ledger Business Premier", "ledger", "business",
     "250.00", False, True, True, False, True, True, 120),
]

# offer_id, product_id, points, spend, days, active, ends_on, rank
#
# Two current travel offers, plus an offer on a withdrawn product and a lapsed
# offer on a current one, so the "current welcome offers" answer depends on both
# the offer's own state and its product's.
WELCOME_OFFERS = [
    ("offer-summit-journey-2026h2", "summit-journey", 40000, "3000.00", 90,
     True, "2026-12-31", 10),
    ("offer-summit-reserve-2026h2", "summit-reserve", 70000, "5000.00", 90,
     True, "2026-12-31", 20),
    ("offer-summit-reserve-elite", "summit-reserve-elite", 100000, "8000.00", 90,
     True, "2026-12-31", 30),
    ("offer-summit-journey-2026h1", "summit-journey", 25000, "2000.00", 60,
     False, "2026-06-30", 40),
    ("offer-summit-reserve-2025h2", "summit-reserve", 60000, "5000.00", 90,
     False, "2025-12-31", 50),
    ("offer-everyday-cash-2026", "everyday-cash", 20000, "1000.00", 90,
     True, "2026-12-31", 60),
    ("offer-everyday-cash-plus-2026", "everyday-cash-plus", 30000, "2000.00", 90,
     True, "2026-12-31", 70),
    ("offer-everyday-cash-legacy", "everyday-cash-legacy", 15000, "1000.00", 60,
     False, "2024-12-31", 80),
    ("offer-horizon-balance-2026", "horizon-balance", 0, "0.00", 0,
     True, "2026-12-31", 90),
    ("offer-horizon-balance-plus-2026", "horizon-balance-plus", 10000, "1500.00", 90,
     True, "2026-12-31", 100),
    ("offer-campus-start-2026", "campus-start", 5000, "500.00", 90,
     True, "2026-12-31", 110),
    ("offer-foundation-secured-2026", "foundation-secured", 2500, "300.00", 90,
     True, "2026-12-31", 120),
    ("offer-ledger-business-2026", "ledger-business", 50000, "6000.00", 90,
     True, "2026-12-31", 130),
    ("offer-ledger-premier-2026", "ledger-business-premier", 90000, "12000.00", 90,
     True, "2026-12-31", 140),
    ("offer-ledger-premier-2025", "ledger-business-premier", 75000, "10000.00", 90,
     False, "2025-12-31", 150),
]


# ---------------------------------------------------------------------------
# knowledge base
# ---------------------------------------------------------------------------

# record_id, effective_at, query_pattern, priority, projection,
# subject_product_id, payload
#
# The thirteen records the four banking conversations retrieve carry priority
# 200; the rest of the base carries 100, so a recorded query that also brushes a
# general record still resolves to the specific one. Patterns are POSIX regular
# expressions matched case-insensitively against the caller's question, and each
# recorded query matches exactly one priority-200 pattern.
#
# effective_at dates sit before this conversation's clock. The base is the same
# design in all four banking tasks, but a record cannot be in force in February
# under a July effective date, so the dates are carried back rather than copied.
#
# The two records that are really views of the product catalog carry a
# projection instead of a frozen copy of it, so a product question the recording
# never asked answers from the same rows the recorded answer came from.
KB_RECORDS = [
    ("card-products-travel-current", "2026-01-15",
     "travel card.*(annual fee|lounge|foreign transaction)", 200,
     "travel_card_matches", None, {}),
    ("summit-reserve-airline-benefits", "2026-01-15",
     "summit reserve.*(checked bag|airline|incidental)", 200,
     "product_airline_benefits", "summit-reserve", {}),
    ("summit-welcome-offers-current", "2026-01-15",
     "welcome (offer|bonus)", 200, "welcome_offers", None,
     {"offers_can_change": True, "approval_guaranteed": False}),
    ("card-application-decision-notice", "2026-01-15",
     "(decision notice|adverse.action|override underwriting)", 200, None, None,
     {"decision_notice": {"explains_factors": True,
                          "may_include_reconsideration_contact": True},
      "phone_agent_can_override_underwriting": False}),
    ("card-application-housing-payment-field", "2026-01-15",
     "housing payment", 200, None, None,
     {"guidance": "Enter the amount the applicant is personally responsible for "
                  "each month and follow the field instructions."}),
    ("card-application-income-field", "2026-01-15",
     "(annual income|salary|freelance income|income.*(field|report))", 200, None, None,
     {"guidance": "Report income the applicant can reasonably access and verify, "
                  "consistent with the application disclosure."}),
    ("profile-email-login-and-notice-routing", "2026-01-15",
     "(login identifier|username).*(email|notice)|email change.*(login|notice routing)",
     200, None, None,
     {"login_identifier_may_remain_same": True,
      "future_notices_use_primary_email": True,
      "unexpected_prompt_guidance": "Use the secure banking site rather than an "
                                    "unexpected message link."}),
    ("referral-qualification-and-posting", "2026-01-15",
     "referral.*(qualif|posting|reward)", 200, None, None,
     {"qualifying_purchase_window_days": 90,
      "posting_window": "up to two billing cycles after qualifying purchase posts",
      "excluded": ["cash advances", "balance transfers", "fees",
                   "returned purchases"],
      "ordinary_retail_purchase": "eligible_if_posted_in_window_and_not_returned",
      "purchase_pending_meanings": ["qualifying purchase has not posted",
                                    "referral match is processing",
                                    "reward has not reached posting stage"]}),
    ("referral-RF8241-offer-version", "2025-12-04",
     "RF8241.*(offer|deadline)|(offer|deadline).*RF8241", 210, None, None,
     {"offer": "$100 statement credit", "deadline_status": "not_passed",
      "exact_deadline": "available_in_tracker"}),
    ("referral-email-mismatch-and-duplicate-applications", "2026-01-15",
     "(email|invitation).*(mismatch|differ)|duplicate application", 200, None, None,
     {"email_mismatch_effect": "may delay automated matching",
      "approved_status_meaning": "application is associated to the referral",
      "reapplication_guidance": "do not reapply; use the referred customer's "
                                "secure account for card questions"}),
    ("8d5fe793-b41b-48c9-aef8-e3ffd9d1d4ab", "2026-01-15",
     "unauthorized transaction", 200, None, None,
     {"steps": ["review household use", "review saved wallets",
                "submit only if unauthorized"],
      "posted_charge_can_be_erased": False,
      "provisional_credit_guaranteed": False,
      "investigation_timing_basis": "transaction_type",
      "post_submit_outputs": ["claim reference",
                              "written timing disclosures in secure messages"]}),
    ("022398d3-51e3-4dcd-b56b-064d53b159f0", "2026-01-15",
     "(unused|unsubmitted).*(session|dispute)", 200, None, None,
     {"unused_unsubmitted_session_waives_future_rights": False,
      "guidance": "Contact the bank promptly if a genuinely unauthorized "
                  "transaction is later discovered."}),
    ("hotel-authorization-holds", "2026-01-15",
     "hotel.*(authorization|hold|incidental)", 200, None, None,
     {"incidental_holds_may_exceed_room_total": True,
      "available_credit_must_cover_full_authorization": True,
      "authorization_guaranteed": False,
      "pending_duration": "may remain pending for a few days after checkout "
                          "depending on merchant finalization"}),

    # The rest of the published base. None of it is retrieved by any recording;
    # it is here so the base has content beyond the recorded questions and so a
    # question the recording never asked gets a real answer.
    ("summit-journey-airline-benefits", "2026-01-15",
     "summit journey.*(checked bag|airline|incidental)", 150,
     "product_airline_benefits", "summit-journey", {}),
    ("summit-reserve-elite-status", "2025-10-18",
     "summit reserve elite", 220, None, None,
     {"guidance": "Summit Reserve Elite is closed to new applications; current "
                  "cardholders keep their existing terms."}),
    ("foreign-transaction-fee-basics", "2025-08-02",
     "foreign transaction fee", 100, None, None,
     {"guidance": "A foreign-transaction fee applies to purchases processed "
                  "outside the United States on products that carry one."}),
    ("lounge-access-program-terms", "2025-09-14",
     "lounge.*(program|terms|guest)", 100, None, None,
     {"guidance": "Lounge membership follows the lounge programme's own terms, "
                  "including guest limits and enrolment steps."}),
    ("cash-back-products-current", "2025-11-22",
     "cash back card", 100, None, None,
     {"guidance": "Everyday Cash and Everyday Cash Plus are the current "
                  "cash-back products."}),
    ("balance-transfer-terms", "2025-07-09",
     "balance transfer", 100, None, None,
     {"guidance": "Balance transfers do not earn rewards and do not count "
                  "towards a welcome offer's required spend."}),
    ("student-card-eligibility", "2025-02-15",
     "student card", 100, None, None,
     {"guidance": "Campus Start requires proof of enrolment at the time of "
                  "application."}),
    ("secured-card-deposit", "2025-03-06",
     "secured card", 100, None, None,
     {"guidance": "Foundation Secured requires a refundable security deposit "
                  "that sets the credit line."}),
    ("business-card-employee-cards", "2025-06-26",
     "business card", 100, None, None,
     {"guidance": "Employee cards on a business account are issued to named "
                  "individuals and share the account's credit line."}),
    ("credit-limit-increase-process", "2025-08-30",
     "credit limit", 100, None, None,
     {"guidance": "A credit-line review is requested in online banking and may "
                  "involve a credit inquiry."}),
    ("credit-report-inquiry-impact", "2025-04-17",
     "credit (report|inquiry|pull)", 100, None, None,
     {"guidance": "An application review may place an inquiry on the "
                  "applicant's credit report."}),
    ("application-status-check", "2025-10-04",
     "application status", 100, None, None,
     {"guidance": "An application's current status is shown in online banking "
                  "and in the decision notice when one is issued."}),
    ("reconsideration-line", "2025-09-27",
     "reconsideration", 100, None, None,
     {"guidance": "A decision notice states whether a reconsideration contact "
                  "is available for that decision."}),
    ("statement-credit-posting", "2025-07-23",
     "statement credit.*(post|appear)", 100, None, None,
     {"guidance": "A statement credit appears on the statement for the cycle in "
                  "which it posts."}),
    ("points-redemption-options", "2025-11-15",
     "(redeem|redemption)", 100, None, None,
     {"guidance": "Points are redeemed in online banking for travel, statement "
                  "credit, or transfers to participating partners."}),
    ("authorization-hold-basics", "2025-08-16",
     "authorization hold", 100, None, None,
     {"guidance": "An authorization hold reduces available credit until the "
                  "merchant finalizes or the hold expires."}),
    ("travel-notice-purpose", "2026-01-15",
     "travel notice", 100, None, None,
     {"guidance": "A travel notice records expected travel and does not "
                  "guarantee that any authorization will be approved.",
      "authorization_guaranteed": False}),
    ("fraud-alert-response", "2026-01-15",
     "fraud alert", 100, None, None,
     {"guidance": "Respond to a fraud alert through online banking or the "
                  "number on the back of the card, not through a link in an "
                  "unexpected message."}),
    ("lost-or-stolen-card-replacement", "2026-01-15",
     "(lost|stolen).*card|card.*(lost|stolen)", 100, None, None,
     {"guidance": "A card reported lost or stolen is closed and replaced; a "
                  "replacement carries a new number."}),
    ("dispute-provisional-credit-timing", "2026-01-15",
     "provisional credit", 100, None, None,
     {"provisional_credit_guaranteed": False,
      "investigation_timing_basis": "transaction_type"}),
    ("secure-message-center", "2025-05-08",
     "secure message", 100, None, None,
     {"guidance": "Secure messages stay inside online banking; ordinary email "
                  "may say that one is waiting but never carries its contents.",
      "unexpected_prompt_guidance": "Use the secure banking site rather than an "
                                    "unexpected message link."}),
    ("paperless-statements", "2025-10-25",
     "(paperless|estatement)", 100, None, None,
     {"guidance": "Paperless statements are delivered to the profile's primary "
                  "email and archived in online banking."}),
    ("autopay-setup", "2025-09-06",
     "(autopay|automatic payment)", 100, None, None,
     {"guidance": "Autopay is set in online banking for the minimum, the "
                  "statement balance, or a fixed amount."}),
    ("late-fee-policy", "2025-07-16",
     "late fee", 100, None, None,
     {"guidance": "A late fee applies when the minimum payment is not received "
                  "by the due date."}),
    ("card-activation", "2025-11-01",
     "(activate|activation)", 100, None, None,
     {"guidance": "A new card is activated in online banking or by the number "
                  "printed on the activation sticker."}),
    ("pin-change-process", "2025-03-27",
     "\\ypin\\y", 100, None, None,
     {"guidance": "A PIN is set through the secure automated line and is never "
                  "spoken to an agent."}),

    # Records that sit deliberately close to a retrieved one, so that matching
    # a question to a pattern has to discriminate rather than merely find
    # something. Each is adjacent in subject to a question one of the four
    # conversations asks: an offer's eligibility exclusions and its expired
    # previous version beside the current offer listing, two further application
    # fields beside the housing-payment field, a fee-exemption record whose
    # pattern the travel-card question also brushes and which loses to it on
    # priority. Retrieval orders by priority, then pattern length, then
    # identifier, so a recorded question still resolves to the recorded record.
    ("welcome-offer-eligibility-exclusions", "2026-01-20",
     "welcome (offer|bonus).*(eligib|exclu|existing cardholder|already have)"
     "|(eligib|exclu).*welcome (offer|bonus)", 230, None, None,
     {"approval_guaranteed": False,
      "offers_can_change": True,
      "excluded": ["balance transfers", "cash advances", "fees",
                   "returned purchases"],
      "guidance": "A welcome offer is available only on a product the "
                  "applicant does not already hold and has not earned an "
                  "offer on within the published look-back period."}),
    ("welcome-offer-previous-version-expired", "2025-04-01",
     "(previous|prior|earlier|last year|2025).*welcome (offer|bonus)"
     "|welcome (offer|bonus).*(previous version|expired|no longer)",
     230, None, None,
     {"offer": "60,000 points after $4,000 of spend in 90 days",
      "deadline_status": "passed",
      "exact_deadline": "2025-12-31",
      "offers_can_change": True,
      "guidance": "This version of the welcome offer has expired; quote the "
                  "current version rather than the one an older invitation "
                  "shows."}),
    ("card-application-employment-status-field", "2025-12-10",
     "employment status", 140, None, None,
     {"guidance": "Select the status that describes the applicant on the "
                  "day of the application; self-employment is reported as "
                  "self-employed rather than as unemployed."}),
    ("card-application-authorized-user-field", "2025-12-10",
     "authorized user.*(application|apply|applying|add)"
     "|(application|apply|applying).*authorized user", 140, None, None,
     {"guidance": "An authorized user may be added during the application "
                  "or afterwards, and adding one does not change who is "
                  "liable for the balance."}),
    ("dispute-chargeback-timeline", "2025-12-20",
     "chargeback"
     "|(dispute|investigation).*(how long|timeline|days|deadline|take)",
     120, None, None,
     {"investigation_timing_basis": "transaction_type",
      "provisional_credit_guaranteed": False,
      "posted_charge_can_be_erased": False,
      "guidance": "Once a dispute is submitted the chargeback follows the "
                  "card network's own timetable, which depends on the "
                  "transaction type rather than on when the customer "
                  "called."}),
    ("expedited-card-replacement-shipping", "2025-08-10",
     "(expedit|rush|overnight).*(card|ship|deliver)"
     "|replacement card.*(ship|arrive|deliver|when)", 130, None, None,
     {"steps": ["confirm the mailing address on the profile",
                "order the replacement",
                "offer expedited delivery where the product allows it"],
      "guidance": "Standard replacement delivery takes several business "
                  "days; expedited delivery is available on some products "
                  "and may carry a fee."}),
    ("foreign-transaction-fee-exemptions", "2025-12-08",
     "foreign transaction fee.*(exempt|waive|which cards|no fee)"
     "|(which cards|no) .*foreign transaction fee", 130, None, None,
     {"guidance": "The travel products carry no foreign-transaction fee; "
                  "the cash-back and student products charge one on "
                  "purchases processed outside the United States."}),
    ("credit-limit-increase-criteria", "2025-11-20",
     "credit (limit|line).*(criteria|qualif|factors|income|approv)"
     "|(criteria|factors).*credit (limit|line)", 130, None, None,
     {"approval_guaranteed": False,
      "guidance": "A credit-line review weighs reported income, payment "
                  "history, and utilisation; meeting the criteria does not "
                  "by itself approve an increase."}),
    ("secure-session-delivery-and-expiry", "2025-11-05",
     "(secure|self.?service) session.*(deliver|channel|link|expire)"
     "|(deliver|channel).*(secure|self.?service) session", 130, None, None,
     {"guidance": "A secure self-service session is delivered to the secure "
                  "message centre or announced by an email notification, "
                  "and its link is reachable only after signing in to "
                  "online banking.",
      "unexpected_prompt_guidance": "Use the secure banking site rather "
                                    "than an unexpected message link."}),
    ("travel-notice-authorization-review", "2026-02-10",
     "travel notice.*(decline|review|guarantee|block|help)"
     "|(declin|review).*travel notice", 130, None, None,
     {"authorization_guaranteed": False,
      "guidance": "A travel notice records that travel is expected; it does "
                  "not lift a review already open on the account and does "
                  "not guarantee that any authorization will be approved."}),
    ("authorized-user-and-joint-account-liability", "2025-09-05",
     "(joint (account|applicant)|authorized user)", 100, None, None,
     {"guidance": "An authorized user may transact on the account without "
                  "being liable for the balance; a joint applicant shares "
                  "liability for it."}),
    ("cash-advance-fee-and-grace-period", "2025-10-12",
     "(cash advance|grace period|interest charge|avoid interest)",
     100, None, None,
     {"excluded": ["cash advances", "balance transfers"],
      "guidance": "A cash advance carries a fee and accrues interest from "
                  "the day it posts, so the grace period that applies to "
                  "purchases does not apply to it."}),
    ("account-closing-effects", "2026-01-28",
     "clos(e|ing|ure).*(account|card)|cancel (my |the |this )?card",
     110, None, None,
     {"steps": ["settle any remaining balance",
                "redeem rewards that would otherwise be forfeited",
                "confirm the closure through secure messages"],
      "guidance": "Closing an account stops new transactions, leaves any "
                  "balance payable under the existing terms, and forfeits "
                  "unredeemed rewards on some products."}),
    ("statement-cycle-and-due-date", "2025-09-23",
     "(billing cycle|statement (date|clos|cycle)|payment due date|due date)",
     100, None, None,
     {"guidance": "A purchase appears on the statement for the cycle in "
                  "which it posts, and the payment due date falls a set "
                  "number of days after that cycle closes."}),
]

WORKFLOW_PROFILES = [
    # workflow, session_slug, resource_suffix_source, resume_supported,
    # save_and_continue, credit_pull_authorized, visible_stages, claim_tracked,
    # access_location, display_label_template, allowed_customer_actions
    ("card_application", "card-application", "none", True, True, False, None,
     False, None, None, None),
    ("referral_status", "referral", "resource_id", True, None, None,
     ["invite", "approval", "qualification", "reward_posting"], False, None, None,
     None),
    ("transaction_dispute", "dispute", "resource_short_ref", True, None, None, None,
     True, "online_banking", "Review {resource_label}",
     ["review", "submit", "close_page"]),
]

DELIVERY_CHANNELS = [
    ("secure_message", "none", "delivered"),
    ("email_notification", "notification_email", "delivered"),
]

NOTIFICATION_TEMPLATES = [
    ("secure_message_waiting", "email", "sent", False),
    ("secure_message_waiting_sms", "sms", "sent", False),
    ("dispute_session_ready", "email", "sent", False),
    ("application_session_ready", "email", "sent", False),
    ("travel_notice_confirmation", "email", "delivered", False),
    ("statement_ready", "email", "delivered", False),
    ("payment_due_reminder", "sms", "sent", False),
]


def build_reference() -> str:
    out = [
        "-- Card catalog, knowledge base, workflow profiles, scenario clock, and\n"
        "-- identifier allocators.\n"
        "-- Generated by environment/gen_seed.py; do not edit by hand.\n\n"
        "BEGIN;\n\n"
    ]

    out.append(insert("scenario", ["key", "value"], [
        ("scenario_time", SCENARIO_TIME),
        ("conversation_started_at", CONVERSATION_STARTED_AT),
        ("conversation_id", CONVERSATION_ID),
        ("domain", "banking"),
        ("timezone", TIMEZONE),
        ("time_status", "available"),
    ]))

    # Identifiers that appear in results are derived from business keys the
    # profile already carries, so only the two genuinely sequential entities
    # need a counter: a verification opened outside any service case, and a
    # specialist transfer.
    out.append(insert("id_allocator",
                      ["entity_type", "scope", "next_value", "template"], [
                          ("identity_verification", "", 1, "verification-{n:06d}"),
                          ("specialist_transfer", "", 1, "specialist-transfer-{n:04d}"),
                      ]))

    out.append(insert("card_products",
                      ["product_id", "product", "family", "category", "annual_fee",
                       "foreign_transaction_fee", "lounge_membership",
                       "airline_incidental_credit", "automatic_free_checked_bag",
                       "airline_specific_rules_apply", "active", "display_rank"],
                      CARD_PRODUCTS))

    out.append(insert("welcome_offers",
                      ["offer_id", "product_id", "points", "spend", "days", "active",
                       "ends_on", "display_rank"], WELCOME_OFFERS))

    out.append(insert(
        "kb_records",
        ["record_id", "effective_at", "query_pattern", "priority", "projection",
         "subject_product_id", "payload"],
        [(rid, eff, pattern, priority, projection, subject, Raw(jsonb(payload)))
         for (rid, eff, pattern, priority, projection, subject, payload)
         in KB_RECORDS]))

    out.append(insert("workflow_profiles",
                      ["workflow", "session_slug", "resource_suffix_source",
                       "resume_supported", "save_and_continue",
                       "credit_pull_authorized", "visible_stages", "claim_tracked",
                       "access_location", "display_label_template",
                       "allowed_customer_actions"], WORKFLOW_PROFILES))

    out.append(insert("delivery_channels",
                      ["channel", "destination_source", "delivered_status"],
                      DELIVERY_CHANNELS))

    out.append(insert("notification_templates",
                      ["template", "channel", "status_on_send",
                       "contains_working_secure_link"], NOTIFICATION_TEMPLATES))

    out.append(insert("card_section_policy", ["section", "disclosure"],
                      SECTION_POLICY))
    out.append(insert("card_section_view",
                      ["scope", "section", "view_index", "fields"], SECTION_VIEWS))

    out.append("COMMIT;\n")
    return "".join(out)


# ---------------------------------------------------------------------------
# population
# ---------------------------------------------------------------------------

FIRST_NAMES = [
    "Johnny", "Daniel", "Justin", "Colin", "Marisol", "Priya", "Andre", "Kelsey",
    "Rosa", "Tomas", "Nadia", "Grant", "Imani", "Victor", "Leah", "Owen", "Farah",
    "Desmond", "Yuki", "Camila", "Errol", "Bianca", "Hugo", "Sana", "Marcus",
    "Elise", "Rahul", "Noor", "Trevor", "Anya", "Jonah", "Celia", "Malik",
    "Renata", "Kwame", "Ingrid", "Silas", "Lorna", "Petra", "Devon",
]

# The four conversations' surnames recur deliberately: a lookup on a surname, or
# on a full name alone, must not resolve to one row.
LAST_NAMES = [
    "Monroe", "Monroe", "Brooks", "Brooks", "Porter", "Porter", "Reeves", "Reeves",
    "Monroy", "Brookshire", "Whitfield", "Okonkwo", "Delgado", "Novak",
    "Bergstrom", "Haddad", "Lindqvist", "Moreau", "Ferraro", "Nakamura",
    "Oyelaran", "Vasquez", "Kaur", "Brennan", "Sorensen", "Achebe", "Marchetti",
    "Kovacs", "Santoro", "Abbasi", "Fontaine", "Reyes", "Thorne", "Duarte",
]

EMAIL_DOMAINS = ["gmail.com", "outlook.com", "email.com", "mailhaven.example",
                 "fastmail.example", "protonmail.example"]

VERIFICATION_PROFILES = [
    ["billing_zip", "mobile_last4"],
    ["billing_zip", "birth_month_day"],
    ["caller_phone", "billing_zip", "card_last4"],
    ["billing_zip", "card_last4"],
]

MERCHANTS = [
    ("grocery", "Riverside Market", "grocery", "RIVERSIDE MKT"),
    ("fuel", "Northgate Fuel", "fuel", "NORTHGATE FUEL"),
    ("hotel", "Harbor View Hotel", "lodging", "HARBOR VIEW HTL"),
    ("airline", "Coastal Air", "airline", "COASTAL AIR 0142"),
    ("logan", "Logan Airport", "travel", "LOGAN AIRPORT F&B"),
    ("marketplace", "Meridian Marketplace", "online marketplace", "MRKTPLC*"),
    ("pharmacy", "Oakline Pharmacy", "pharmacy", "OAKLINE RX"),
    ("hardware", "Fenwick Hardware", "home improvement", "FENWICK HDW"),
    ("streaming", "Lumen Streaming", "digital goods", "LUMEN STREAM"),
    ("restaurant", "Bellweather Kitchen", "restaurant", "BELLWEATHER KIT"),
    ("rideshare", "Transit Now", "transportation", "TRANSIT NOW"),
    ("parking", "Airside Parking", "parking", "AIRSIDE PKG"),
]

CITIES = [
    "Portland, Maine", "Boston, Massachusetts", "Raleigh, North Carolina",
    "Kansas City, Missouri", "Philadelphia, Pennsylvania", "Washington, DC",
    "Austin, Texas", "Denver, Colorado", "Chicago, Illinois", "Seattle, Washington",
    "Miami, Florida", "Phoenix, Arizona",
]

DECLINE_REASONS = ["travel_review", "prior_review_open", "insufficient_credit",
                   "card_restricted", "suspected_fraud", "expired_card"]

RESTRICTION_KINDS = [
    ("travel_review", True),
    ("fraud_review", True),
    # Neither of these is lifted by confirming activity, so a resolve attempt on
    # one is refused rather than quietly succeeding.
    ("delinquency_hold", False),
    ("lost_card_block", False),
]

CASE_KINDS = [
    ("email_change", "email-change"),
    ("referral", "referral"),
    ("dispute", "dispute"),
    ("card", "card"),
    ("statement", "statement"),
    ("payment", "payment"),
]


def build_population() -> str:
    rng = random.Random(RNG_SEED + 1)
    out = [
        "-- Customers, cards, ledger activity, restrictions, referrals, sessions,\n"
        "-- notifications, and verification records.\n"
        "-- Generated by environment/gen_seed.py; do not edit by hand.\n"
        "-- The entities this conversation touches are in 004_scenario.sql.\n\n"
        "BEGIN;\n\n"
    ]

    product_ids = [p[0] for p in CARD_PRODUCTS if p[10]]
    counts: dict = {}

    # -- customers ----------------------------------------------------------
    customers, channels, cases = [], [], []
    used_account_ids = set()
    used_emails = set()
    for index in range(100):
        first = rng.choice(FIRST_NAMES)
        last = rng.choice(LAST_NAMES)
        customer_id = f"customer-{first.lower()}-{last.lower()}-{index:03d}"
        account_id = f"SF{rng.randint(200000, 299999)}"
        while account_id in used_account_ids or account_id == "SF204771":
            account_id = f"SF{rng.randint(200000, 299999)}"
        used_account_ids.add(account_id)

        email = f"{first.lower()}.{last.lower()}{index}@{rng.choice(EMAIL_DOMAINS)}"
        if email in used_emails:
            continue
        used_emails.add(email)

        required = rng.choice(VERIFICATION_PROFILES)
        if "caller_phone" not in required:
            caller_match = None
        else:
            # A profile that counts the calling channel and whose call did not
            # arrive on it cannot be verified on that factor alone.
            caller_match = rng.random() > 0.3
        customers.append((
            customer_id, account_id, f"{first} {last}", last,
            account_id, f"{first.lower()}{index}",
            email, email, f"{rng.randint(10000, 99999)}",
            rng.randint(1, 12), rng.randint(1, 28), f"{rng.randint(1000, 9999)}",
            caller_match, required,
            "email" if rng.random() < 0.2 else "username",
        ))

        # Roughly one profile in eight has an enrolled channel whose owner never
        # completes a challenge, so a confirmation poll on it stays at 'sent'.
        for slot in range(rng.choice([0, 1, 1, 1, 2])):
            enrolled = rng.random() > 0.15
            completes = enrolled and rng.random() > 0.12
            channels.append((
                f"{customer_id}-channel-{slot}", customer_id,
                rng.choice(["sms", "sms", "secure_message"]),
                "***-***-" + f"{rng.randint(1000, 9999)}",
                enrolled, completes,
                SCENARIO_TIME if completes else None,
            ))

        if rng.random() < 0.7:
            kind, slug = rng.choice(CASE_KINDS)
            cases.append((f"{customer_id}-case", customer_id, kind, slug,
                          "open" if rng.random() < 0.55 else "closed",
                          CONVERSATION_STARTED_AT))

    out.append(insert("customers",
                      ["customer_id", "account_id", "full_name", "family_name",
                       "verification_key", "notice_slug", "primary_email",
                       "notification_email", "billing_zip", "birth_month",
                       "birth_day", "mobile_last4", "caller_channel_match",
                       "required_verification_methods", "login_identifier_kind"],
                      customers))
    out.append(insert("trusted_channels",
                      ["channel_id", "customer_id", "type", "masked_destination",
                       "enrolled", "confirmation_completes",
                       "confirmation_verified_at"], channels))
    out.append(insert("service_cases",
                      ["case_id", "customer_id", "case_kind", "case_slug", "status",
                       "opened_at"], cases))
    counts.update(customers=len(customers), trusted_channels=len(channels),
                  service_cases=len(cases))

    # -- identity verifications and confirmations ---------------------------
    verifications, confirmations = [], []
    for customer in customers:
        if rng.random() > 0.4:
            continue
        customer_id, required = customer[0], customer[13]
        verified = rng.random() > 0.25
        matched = list(required) if verified else list(required[:-1])
        verifications.append((
            f"verification-{customer[1]}-prior", customer_id,
            "verified" if verified else "unverified", required, matched,
            CONVERSATION_STARTED_AT if verified else None,
            CONVERSATION_STARTED_AT if verified else None, False,
        ))
    enrolled_channels = [c for c in channels if c[4]]
    for channel in rng.sample(enrolled_channels, min(25, len(enrolled_channels))):
        channel_id, customer_id = channel[0], channel[1]
        state = rng.choice(["sent", "verified", "verified", "expired"])
        confirmations.append((
            f"confirmation-email-change-{channel_id}", customer_id, channel_id,
            "email_change", channel[3], state, None,
            CONVERSATION_STARTED_AT,
            CONVERSATION_STARTED_AT if state == "verified" else None,
            CONVERSATION_STARTED_AT,
        ))
    out.append(insert("identity_verifications",
                      ["verification_id", "customer_id", "status", "required_methods",
                       "matched_methods", "verified_at", "expires_at",
                       "time_asserted"], verifications))
    out.append(insert("channel_confirmations",
                      ["confirmation_id", "customer_id", "channel_id", "purpose",
                       "masked_destination", "status", "verification_id", "sent_at",
                       "verified_at", "expires_at"], confirmations))
    counts.update(identity_verifications=len(verifications),
                  channel_confirmations=len(confirmations))

    # -- cards and ledger ---------------------------------------------------
    cards, ledger, restrictions, links, notices = [], [], [], [], []
    used_last4 = set()
    ledger_index = 0
    for customer in customers:
        customer_id = customer[0]
        for slot in range(rng.choice([2, 2, 2, 3, 3, 3, 4])):
            last4 = f"{rng.randint(1000, 9999)}"
            while (customer_id, last4) in used_last4:
                last4 = f"{rng.randint(1000, 9999)}"
            used_last4.add((customer_id, last4))
            card_id = f"card-{customer_id.replace('customer-', '')}-{last4}"
            limit = rng.choice([2000, 3500, 5000, 8000, 12000, 20000])
            available = round(limit * rng.uniform(0.05, 0.95), 2)
            restricted = rng.random() < 0.12
            cards.append((
                card_id, customer_id, last4, rng.choice(product_ids),
                "temporarily_restricted" if restricted else "active",
                rng.random() < 0.05,
                rng.choice(["current"] * 8 + ["past_due", "in_collections"]),
                f"{limit}.00", f"{available:.2f}",
            ))

            for _ in range(rng.choice([1, 2, 2, 2, 3, 3])):
                ledger_index += 1
                key, merchant, category, descriptor = rng.choice(MERCHANTS)
                amount = round(rng.uniform(4, 900), 2)
                kind = rng.choices(["posted", "authorization", "decline"],
                                   weights=[6, 3, 1])[0]
                occurred = dt.datetime(LEDGER_YEAR, LEDGER_MONTH,
                                       rng.randint(1, LEDGER_LAST_DAY),
                                       rng.randint(7, 21), rng.choice([0, 5, 18, 32, 47]))
                transaction_id = f"transaction-{ledger_index:05d}"
                short_ref = transaction_id[-4:]
                suffix = f"{rng.randint(1000, 9999)}"
                while suffix in RESERVED_DESCRIPTOR_SUFFIXES:
                    suffix = f"{rng.randint(1000, 9999)}"
                ledger.append((
                    transaction_id, card_id, kind, key, merchant,
                    rng.choice(CITIES) if rng.random() > 0.25 else None,
                    descriptor + (suffix if descriptor.endswith("*") else ""),
                    category, f"{amount:.2f}",
                    {"posted": "posted", "authorization": "approved",
                     "decline": "declined"}[kind],
                    rng.choice(DECLINE_REASONS) if kind == "decline" else None,
                    {"posted": "settled", "authorization": "pending",
                     "decline": "not_applicable"}[kind],
                    occurred.strftime(f"%Y-%m-%dT%H:%M:00{LEDGER_OFFSET}"),
                    occurred.date().isoformat() if kind == "posted" else None,
                    f"{rng.uniform(1, 5):.2f}" if kind == "posted" and rng.random() < 0.2
                    else None,
                    f"transaction ending {short_ref}", short_ref,
                ))

            if restricted or rng.random() < 0.06:
                kind, resolvable = rng.choice(RESTRICTION_KINDS)
                restriction_id = f"restriction-{card_id}-{kind.replace('_', '-')}"
                # An open restriction and a restricted card are the same fact
                # seen twice, so an active card only carries resolved history.
                removed = not restricted
                restrictions.append((
                    restriction_id, card_id, kind,
                    "removed" if removed else "open", resolvable,
                    CONVERSATION_STARTED_AT,
                    CONVERSATION_STARTED_AT if removed else None,
                ))
                own = [row for row in ledger if row[1] == card_id]
                for rank, row in enumerate(own[:2]):
                    links.append((restriction_id, row[0], rank, None))

            if rng.random() < 0.18:
                destination = rng.choice(CITIES)
                notices.append((
                    f"travel-notice-{card_id}-{destination.split(',')[0].lower()}",
                    card_id, [destination],
                    (dt.date(LEDGER_YEAR, TRAVEL_MONTH,
                             rng.randint(1, 28))).isoformat(), False,
                    rng.choice(["created"] * 6 + ["expired", "cancelled"]),
                    CONVERSATION_STARTED_AT,
                ))

    out.append(insert("card_accounts",
                      ["card_id", "customer_id", "card_last4", "product_id", "status",
                       "reported_lost", "payment_status", "credit_limit",
                       "available_credit"], cards))
    out.append(insert("transactions",
                      ["transaction_id", "card_id", "kind", "merchant_key", "merchant",
                       "merchant_location", "descriptor", "category", "amount",
                       "status", "reason", "settlement_state", "occurred_at",
                       "posted_date", "preceded_by_authorization_amount",
                       "resource_label", "short_ref"], ledger))
    out.append(insert("card_restrictions",
                      ["restriction_id", "card_id", "kind", "status",
                       "customer_resolvable", "opened_at", "resolved_at"],
                      restrictions))
    out.append(insert("restriction_transactions",
                      ["restriction_id", "transaction_id", "link_rank", "confirmed_at"],
                      links))
    out.append(insert("travel_notices",
                      ["notice_id", "card_id", "destinations", "return_date",
                       "authorization_guaranteed", "status", "created_at"], notices))
    counts.update(card_accounts=len(cards), transactions=len(ledger),
                  card_restrictions=len(restrictions), travel_notices=len(notices))

    # -- referrals ----------------------------------------------------------
    referrals = []
    for index, customer in enumerate(customers):
        for slot in range(rng.choice([0, 0, 1, 1, 2, 3])):
            month = rng.choice(REFERRAL_MONTHS)
            invited = dt.date(LEDGER_YEAR if month <= LEDGER_MONTH else LEDGER_YEAR - 1,
                              month, rng.randint(1, 28))
            application = rng.choice(["invited", "applied", "approved", "approved",
                                      "declined", "expired"])
            qualification = ("purchase_pending" if application != "approved"
                             else rng.choice(["purchase_pending", "qualified",
                                              "not_qualified"]))
            referrals.append((
                f"RF{7000 + index * 7 + slot}", customer[0],
                f"{invited.strftime('%B')} {invited.day}", invited.isoformat(),
                rng.choice(["email", "email", "sms"]),
                f"{customer[3][0].lower()}-{customer[3].lower()}\u2026",
                application, qualification,
                rng.choice(["$100 statement credit", "$150 statement credit",
                            "20,000 points", "$50 statement credit"]),
                None, (invited + dt.timedelta(days=90)).isoformat(), 100 + slot,
            ))
    out.append(insert("referrals",
                      ["referral_id", "referring_customer_id", "invited_at_display",
                       "invited_on", "invited_channel", "invited_masked",
                       "application_status", "qualification_status", "offer",
                       "offer_version_record_id", "deadline_on", "display_rank"],
                      referrals))
    counts.update(referrals=len(referrals))

    # -- sessions, deliveries, notifications --------------------------------
    sessions, deliveries, notifications = [], [], []
    session_index = 0
    for customer in customers:
        if rng.random() > 0.58:
            continue
        session_index += 1
        customer_id, email = customer[0], customer[7]
        workflow = rng.choice(["card_application", "referral_status",
                               "transaction_dispute"])
        owned_referrals = [r[0] for r in referrals if r[1] == customer_id]
        owned_cards = [c[0] for c in cards if c[1] == customer_id]
        owned_ledger = [row[0] for row in ledger if row[1] in owned_cards]
        if workflow == "referral_status" and not owned_referrals:
            workflow = "card_application"
        if workflow == "transaction_dispute" and not owned_ledger:
            workflow = "card_application"

        if workflow == "card_application":
            resource = rng.choice(product_ids)
            session_id = f"session-card-application-{session_index:03d}"
            save, credit, stages, claim = True, False, None, False
            access, label, actions = None, None, None
        elif workflow == "referral_status":
            resource = rng.choice(owned_referrals)
            session_id = f"session-referral-{resource}"
            save, credit, stages, claim = None, None, [
                "invite", "approval", "qualification", "reward_posting"], False
            access, label, actions = None, None, None
        else:
            resource = rng.choice(owned_ledger)
            session_id = f"session-dispute-{resource[-4:]}-{session_index:03d}"
            save, credit, stages, claim = None, None, None, True
            access, label = "online_banking", f"Review transaction ending {resource[-4:]}"
            actions = ["review", "submit", "close_page"]

        # A spread of session states, including sessions nobody opened, so a
        # status read is a real question rather than a formality.
        status = rng.choice(["issued", "issued", "open_not_submitted", "saved",
                             "submitted", "expired", "closed"])
        sessions.append((
            session_id, customer_id, workflow, resource, status,
            status == "submitted", True, save, credit, claim,
            f"claim-{session_index:05d}" if claim and status == "submitted" else None,
            access, label, actions, stages, rng.random() > 0.3,
            CONVERSATION_STARTED_AT,
            CONVERSATION_STARTED_AT if status not in ("issued",) else None,
        ))
        for rank, channel in enumerate(rng.choice(
                [["secure_message"], ["secure_message", "email_notification"]])):
            deliveries.append((
                session_id, channel, rank, "delivered",
                f"{email[0]}***@{email.split('@')[1]}"
                if channel == "email_notification" else None,
            ))
        if rng.random() < 0.8:
            notifications.append((
                f"notification-{session_id.replace('session-', '')}", customer_id,
                session_id, "email", "secure_message_waiting", "sent",
                f"{email[0]}***@{email.split('@')[1]}", False,
                CONVERSATION_STARTED_AT,
            ))

    out.append(insert("self_service_sessions",
                      ["session_id", "customer_id", "workflow", "resource_id",
                       "status", "submitted", "resume_supported", "save_and_continue",
                       "credit_pull_authorized", "claim_tracked", "claim_id",
                       "access_location", "display_label", "allowed_customer_actions",
                       "visible_stages", "customer_opens", "issued_at", "opened_at"],
                      sessions))
    out.append(insert("session_deliveries",
                      ["session_id", "channel", "delivery_rank", "status",
                       "masked_destination"], deliveries))
    out.append(insert("notifications",
                      ["notification_id", "customer_id", "related_resource_id",
                       "channel", "template", "status", "masked_destination",
                       "contains_working_secure_link", "sent_at"], notifications))
    counts.update(self_service_sessions=len(sessions),
                  session_deliveries=len(deliveries),
                  notifications=len(notifications))

    out.append("COMMIT;\n")
    print("population:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return "".join(out)


def main() -> None:
    os.makedirs(SQL, exist_ok=True)
    reference = build_reference()
    population = build_population()
    with open(os.path.join(SQL, "002_reference.sql"), "w") as fh:
        fh.write(reference)
    with open(os.path.join(SQL, "003_population.sql"), "w") as fh:
        fh.write(population)
    print(f"wrote 002_reference.sql ({len(reference)} bytes) and "
          f"003_population.sql ({len(population)} bytes)")


if __name__ == "__main__":
    main()
