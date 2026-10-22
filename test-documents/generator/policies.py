"""Fictional Rahmqvist policies for testing the assistant in an FAQ setting.

    python3 -I test-documents/generator/policies.py test-documents

Every figure is invented, but kept consistent across documents. Each document
carries a TEST DOCUMENT banner and footer so it can never pass for a real one.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mkpdf import Doc  # noqa: E402

EN_BANNER = ("TEST DOCUMENT", "Fictional content created to test the Rahmqvist Assistant. "
             "It is not an actual Rahmqvist policy.")
SV_BANNER = ("TESTDOKUMENT", "Fiktivt innehåll som skapats för att testa Rahmqvist Assistant. "
             "Det är inte en faktisk policy från Rahmqvist.")


def start(title: str, code: str, version: str, meta: str, banner=EN_BANNER, footer_word="TEST DOCUMENT - fictional content") -> Doc:
    doc = Doc(title, f"{footer_word}. {title}, {code} v{version}", subject="Test policy for the Rahmqvist Assistant")
    doc.heading_title(title)
    doc.meta(meta)
    doc.banner(*banner)
    return doc


def travel(out: Path) -> None:
    d = start("Travel and Expense Policy", "FIN-03", "2026.2",
              "Policy FIN-03 · Version 2026.2 · Effective 1 October 2026 · Owner: Group Finance · Rahmqvist Group")
    d.h("1. Purpose")
    d.p("This policy sets out how business travel is approved, booked and paid for, and how expenses are claimed. "
        "The aim is safe, comfortable and cost-conscious travel, with the same rules for everyone.")
    d.h("2. Who this policy applies to")
    d.p("This policy applies to all employees of the Rahmqvist Group in every country. A country may publish an "
        "addendum with local rates, such as per diems and mileage. Where an addendum differs from this policy, the "
        "addendum applies in that country - for example the Travel and Expense Addendum - Sweden (FIN-03-SE).")
    d.h("3. Before you travel")
    d.bullets([
        "Every business trip must be approved by your line manager in the travel portal before anything is booked.",
        "Trips that cost more than EUR 5,000 in total, or go outside Europe, also need your department head's approval.",
        "Book flights at least 14 days ahead where possible; fares booked later must be explained in the request.",
        "Register every trip in the travel portal, so the company can reach you in an emergency.",
    ])
    d.h("4. Flights and trains")
    d.p("Take the train for journeys under four (4) hours door to door. For flights, book economy class. Premium "
        "economy is allowed on flights longer than six (6) hours. Business class needs the CFO's written approval "
        "in advance. One checked bag per flight is reimbursed, and seat selection up to EUR 25 per flight.")
    d.h("5. Hotels")
    d.p("Book hotels through the travel portal. The nightly limit is EUR 180, or EUR 250 in London, Paris, New York "
        "and Zurich, including taxes. Choose a rate with breakfast where it costs no more. Private rentals such as "
        "holiday apartments are not allowed, because they are not covered by our duty-of-care arrangements.")
    d.h("6. Meals and per diems")
    d.p("Unless a country addendum sets a per diem, actual meal costs are reimbursed up to EUR 60 per travel day, "
        "with receipts. Alcohol is not reimbursed, except as part of client entertainment within the limits below.")
    d.h("7. Client entertainment")
    d.p("Entertaining clients is allowed up to EUR 90 per person. The expense claim must name everyone who attended "
        "and state the business purpose. An event costing more than EUR 500 in total needs your manager's approval "
        "beforehand. Gifts to clients follow the Code of Conduct (LEG-02).")
    d.h("8. Getting around")
    d.p("Use public transport where practical; taxis and ride-hailing are fine when they are safer or save "
        "significant time. Rental cars are economy class, and only when cheaper than the alternatives. Using your "
        "own car for business is reimbursed at EUR 0.30 per kilometre, unless a country addendum sets another rate.")
    d.h("9. Corporate card and expense claims")
    d.bullets([
        "Use your corporate card for business expenses whenever possible. It must never be used for private purchases.",
        "Submit expenses in the expense app within 30 days of your return. A photo of the receipt is enough.",
        "A receipt is required for every expense over EUR 10.",
        "Claims submitted more than 90 days after the expense are not reimbursed.",
        "Claims approved by the 20th of the month are paid with that month's salary.",
    ])
    d.p("If you lose a receipt, file a missing-receipt declaration in the expense app. This is accepted at most "
        "twice per calendar year, and for no more than EUR 75 each time.")
    d.h("10. What is not reimbursed")
    d.p("Fines and parking tickets, minibar purchases, personal entertainment, upgrades you choose yourself, "
        "laundry on trips shorter than five (5) nights, and lost luggage beyond what insurance covers.")
    d.h("11. Combining business and leisure")
    d.p("You may add holiday days to a business trip, provided the flight does not cost more because of it. Extra "
        "hotel nights and other private costs are your own, and travel insurance covers only the business part.")
    d.h("12. Safety and insurance")
    d.p("Business travel insurance covers every approved trip worldwide. Your insurance card, with the 24/7 "
        "assistance number, is in the HR portal. If you fall ill or something goes wrong, call the assistance number "
        "first, then tell your manager.")
    d.h("Summary of the rules")
    d.table(["Topic", "Rule"], [
        ["Approval", "Line manager, before booking; department head over EUR 5,000 or outside Europe"],
        ["Train or plane", "Train for journeys under 4 hours"],
        ["Flight class", "Economy; premium economy over 6 hours; business only with CFO approval"],
        ["Hotel limit", "EUR 180 per night; EUR 250 in London, Paris, New York, Zurich"],
        ["Meals", "Actual cost up to EUR 60 per day, unless a country per diem applies"],
        ["Client entertainment", "Up to EUR 90 per person; over EUR 500 in total needs approval"],
        ["Own car", "EUR 0.30 per km, unless a country addendum says otherwise"],
        ["Expense deadline", "30 days after return; nothing paid after 90 days"],
        ["Receipts", "Required over EUR 10; missing-receipt declaration at most twice a year, max EUR 75"],
    ], [0.3, 0.7])
    d.h("Frequently asked questions")
    d.qa([
        ("Can I fly business class to Singapore?",
         "The flight is longer than six hours, so premium economy is allowed. Business class needs the CFO's "
         "written approval before you book."),
        ("I lost a receipt. What do I do?",
         "File a missing-receipt declaration in the expense app. It is accepted twice a year, up to EUR 75 each time."),
        ("When will I get my money back?",
         "Claims approved by the 20th of the month are paid with that month's salary; later ones with the next."),
        ("Do I get a per diem?",
         "Under this policy, meals are reimbursed at actual cost up to EUR 60 per day. Some countries pay a per diem "
         "instead - see your country's addendum, such as FIN-03-SE for Sweden."),
        ("Can I stay a few extra days for a holiday?",
         "Yes, if the flight does not cost more. Extra nights are at your own cost, and insurance covers only the "
         "business part of the trip."),
        ("Can I book an apartment instead of a hotel?",
         "No. Private rentals are not allowed, because they fall outside our duty-of-care arrangements."),
    ])
    d.h("13. Questions")
    d.p("Questions about this policy go to Group Finance at travel@rahmqvist.example.")
    d.save(out / "travel-and-expense-policy.pdf")


def travel_se(out: Path) -> None:
    d = start("Travel and Expense Addendum - Sweden", "FIN-03-SE", "2026.1",
              "Addendum FIN-03-SE · Version 2026.1 · Effective 1 October 2026 · Owner: Finance Sweden · "
              "Rahmqvist Sverige AB")
    d.h("1. Purpose and precedence")
    d.p("This addendum sets the Swedish rates for business travel. It applies to employees on a Swedish employment "
        "contract. Where it differs from the Group Travel and Expense Policy (FIN-03), this addendum applies in Sweden. "
        "Everything it does not mention follows FIN-03.")
    d.h("2. Mileage for your own car")
    d.p("Business driving in your own car is reimbursed at SEK 30 per Swedish mil (10 km) instead of the Group rate. "
        "SEK 25 per mil is tax-free under the Swedish Tax Agency's rules; the remaining SEK 5 per mil is paid as "
        "taxable salary. Log every trip in the expense app with date, start and end point and purpose.")
    d.h("3. Per diem (traktamente)")
    d.p("Instead of actual meal costs, employees in Sweden receive a per diem on business trips that include an "
        "overnight stay at least 50 km from both home and their usual workplace.")
    d.bullets([
        "Full day in Sweden: SEK 300.",
        "Half day in Sweden: SEK 150 - for a departure day starting after 12:00 or a return day ending before 19:00.",
        "Trips abroad: the Swedish Tax Agency's standard amount for each country.",
        "Meals that are paid for reduce the per diem: breakfast by 20%, lunch by 35% and dinner by 35%.",
    ])
    d.h("4. Hotels in Sweden")
    d.p("The nightly limit in Sweden is SEK 1,900 in Stockholm, Gothenburg and Malmö, and SEK 1,500 elsewhere, "
        "including VAT.")
    d.h("5. Trains")
    d.p("Second class is the standard. First class is allowed on journeys longer than three (3) hours when it is "
        "booked at least 14 days ahead.")
    d.h("6. Taxis")
    d.p("Use the company taxi account, which bills Rahmqvist directly; you then do not need to claim the fare. "
        "Other taxis are reimbursed with a receipt.")
    d.h("7. Keeping receipts")
    d.p("Swedish accounting law requires receipts to be kept for seven years. The expense app archives them for you, "
        "so you do not need to keep paper copies once the photo is uploaded.")
    d.h("Summary of the Swedish rates")
    d.table(["Topic", "Rule in Sweden", "Group rule it replaces"], [
        ["Own car", "SEK 30 per mil (SEK 25 tax-free)", "EUR 0.30 per km"],
        ["Meals", "Per diem: SEK 300 full day, SEK 150 half day", "Actual cost up to EUR 60 per day"],
        ["Hotel limit", "SEK 1,900 in Stockholm, Gothenburg, Malmö; SEK 1,500 elsewhere", "EUR 180 per night"],
        ["Trains", "2nd class; 1st class over 3 hours if booked 14 days ahead", "Train for journeys under 4 hours"],
    ], [0.2, 0.45, 0.35])
    d.h("Frequently asked questions")
    d.qa([
        ("What do I get for driving 120 km to a client in my own car?",
         "12 mil at SEK 30 is SEK 360, of which SEK 300 is tax-free and SEK 60 is paid as taxable salary."),
        ("Is the Group's rate of EUR 0.30 per km used in Sweden?",
         "No. In Sweden this addendum applies, so the rate is SEK 30 per mil."),
        ("I travel to Gothenburg on Monday at 08:00 and come back Tuesday at 17:00. What per diem do I get?",
         "Monday is a full day (SEK 300) because you leave before 12:00, and Tuesday is a half day (SEK 150) "
         "because you are back before 19:00 - SEK 450 in total, less any meals paid for."),
        ("The hotel included breakfast. Does that matter?",
         "Yes. A breakfast that is paid for reduces that day's per diem by 20%."),
    ])
    d.h("8. Questions")
    d.p("Questions about this addendum go to Finance Sweden at finance.se@rahmqvist.example.")
    d.save(out / "travel-and-expense-addendum-sweden.pdf")


def it_policy(out: Path) -> None:
    d = start("IT Acceptable Use and Information Security Policy", "IT-01", "2026.3",
              "Policy IT-01 · Version 2026.3 · Effective 15 August 2026 · Owner: IT Security · Rahmqvist Group")
    d.h("1. Purpose")
    d.p("This policy explains how to use company IT safely, so that client and company information stays protected. "
        "It applies to all employees, consultants and contractors who use Rahmqvist systems or devices.")
    d.h("2. Your account")
    d.bullets([
        "Multi-factor authentication (MFA) is required for every company system, from any location.",
        "Passwords must be at least 14 characters; a passphrase of four or more words is recommended.",
        "Never reuse your company password anywhere else. A password manager is provided for free.",
        "Passwords do not expire. Change yours at once if you suspect anyone else knows it.",
        "Never share your account, and never let anyone else approve an MFA prompt for you.",
    ])
    d.h("3. Devices")
    d.p("Company laptops are encrypted and managed by IT. The screen locks automatically after five (5) minutes "
        "without activity; lock it yourself whenever you step away. Install updates within seven (7) days of being "
        "prompted. Company devices are for you only and must not be used by family members or friends. You may read "
        "company email on a personal phone, but only through the company's managed apps.")
    d.h("4. Software and cloud services")
    d.p("Install software only from the self-service catalogue. To request something that is not there, raise a "
        "ticket with the IT Service Desk. Free trials of online services need IT's approval, because they often "
        "store data outside the EU. Company files must never be stored in personal cloud storage or personal email.")
    d.h("5. AI tools")
    d.p("Use only the approved enterprise AI tools listed on the intranet, including the Rahmqvist Assistant. Never "
        "paste confidential, client or personal data into public AI tools. You remain responsible for checking "
        "anything an AI tool produces before you use or share it.")
    d.h("6. Email and phishing")
    d.p("Report suspicious emails with the Report Phishing button in your mail client. IT will never ask for your "
        "password, by email, chat or phone. Never enter your credentials on a page you reached from a link in an "
        "email; go to the service directly instead.")
    d.h("7. Classifying information")
    d.table(["Class", "Examples", "How to handle it"], [
        ["Public", "Published marketing material", "No restrictions"],
        ["Internal", "Policies, internal news", "Share within Rahmqvist only"],
        ["Confidential", "Client documents, contracts, salaries", "Need-to-know; encrypted when sent outside the company"],
        ["Strictly confidential", "M&A plans, security incidents", "Named people only; never on paper outside the office"],
    ], [0.22, 0.38, 0.40])
    d.h("8. Lost or stolen devices")
    d.p("Report a lost or stolen laptop or phone to the IT Service Desk within one (1) hour of noticing - the desk is "
        "open around the clock. IT will lock and, if needed, wipe the device remotely. If it was stolen, also report "
        "the theft to the police within 24 hours and add the reference number to the ticket.")
    d.h("9. USB drives and removable media")
    d.p("USB storage devices are blocked on company laptops. Share files through company file storage instead. "
        "Keyboards, mice, headsets and screens work as normal.")
    d.h("10. Working in public places")
    d.p("Use a privacy screen on trains, planes and in cafés, and do not hold confidential calls where others can "
        "listen. Use your phone's hotspot rather than public Wi-Fi when you work with confidential information.")
    d.h("11. Personal use and monitoring")
    d.p("Limited personal use of company IT is allowed, as long as it does not affect your work, is legal and does "
        "not put company information at risk. For security, system logs are kept for 90 days and may be reviewed "
        "when investigating an incident. Breaking this policy may lead to disciplinary action.")
    d.h("Summary of the rules")
    d.table(["Topic", "Rule"], [
        ["MFA", "Required everywhere"],
        ["Password", "At least 14 characters; no forced expiry; never reused"],
        ["Screen lock", "Automatic after 5 minutes"],
        ["Updates", "Within 7 days"],
        ["Lost or stolen device", "Report to IT within 1 hour; police report within 24 hours if stolen"],
        ["USB storage", "Blocked"],
        ["AI tools", "Approved tools only; no confidential data in public tools"],
        ["Security logs", "Kept for 90 days"],
    ], [0.3, 0.7])
    d.h("Frequently asked questions")
    d.qa([
        ("I clicked a link in a phishing email. What should I do?",
         "Disconnect from the network, then call the IT Service Desk straight away. If you entered your password, "
         "change it from another device. Reporting quickly matters far more than having clicked."),
        ("Can I use ChatGPT for work?",
         "Only approved enterprise AI tools may be used, and public AI tools must never receive confidential, client "
         "or personal data. Check the intranet for the approved list."),
        ("I left my laptop on the train. Who do I call?",
         "The IT Service Desk, within one hour - it is open 24/7. They will lock the laptop remotely."),
        ("Can I copy files to my own USB stick?",
         "No. USB storage is blocked on company laptops; use company file storage to share files."),
        ("How often do I have to change my password?",
         "Never on a schedule. Change it only if you think someone else may know it."),
        ("Can my partner use my work laptop to check something quickly?",
         "No. Company devices are for your use only."),
    ])
    d.h("12. Questions")
    d.p("Contact the IT Service Desk at servicedesk@rahmqvist.example, or on extension 4444 from any office phone.")
    d.save(out / "it-acceptable-use-and-security-policy.pdf")


def conduct(out: Path) -> None:
    d = start("Code of Conduct: Gifts, Hospitality and Conflicts of Interest", "LEG-02", "2026.1",
              "Policy LEG-02 · Version 2026.1 · Effective 1 June 2026 · Owner: Legal & Compliance · Rahmqvist Group")
    d.h("1. Purpose")
    d.p("Clients trust us because our decisions are fair and independent. This policy sets clear limits on gifts and "
        "hospitality and explains how to handle conflicts of interest, so that no one can question that trust. "
        "It applies to everyone working for the Rahmqvist Group.")
    d.h("2. Gifts you receive")
    d.bullets([
        "You may accept gifts worth up to EUR 50, in total per giver per calendar year.",
        "Register every gift worth more than EUR 25 in the gift register within five (5) working days.",
        "Never accept cash, vouchers or gift cards, whatever their value.",
        "While a tender or procurement is open, accept nothing at all from anyone taking part in it.",
        "Politely return a gift above the limit, or hand it to Compliance, who will donate it to charity.",
    ])
    d.h("3. Gifts you give")
    d.p("The same EUR 50 limit applies to gifts you give on the company's behalf. Public officials may receive "
        "branded items worth less than EUR 10 and nothing else.")
    d.h("4. Hospitality")
    d.p("Ordinary business meals are fine up to EUR 120 per person. When the host does not attend, an invitation "
        "counts as a gift and the gift limit applies. Travel or accommodation paid for by a supplier or client always "
        "needs Compliance's approval in advance.")
    d.h("5. Conflicts of interest")
    d.p("Declare in the HR portal, within 30 days of it arising, any situation where your private interests could "
        "affect your judgement, for example:")
    d.bullets([
        "a close personal relationship with someone at a supplier, a client or a competitor;",
        "a close relationship with a colleague in your reporting line;",
        "owning more than 1% of a supplier, client or competitor;",
        "a board seat or advisory role in another company.",
    ])
    d.p("Declaring a conflict is not wrongdoing; hiding one is. Your manager and Compliance will agree with you how "
        "to manage it, for example by having someone else take the decisions involved.")
    d.h("6. Side jobs")
    d.p("A second job or business needs your manager's written approval in advance. It must not compete with "
        "Rahmqvist, use company resources or information, or affect your working hours.")
    d.h("7. Bribery and corruption")
    d.p("We have zero tolerance for bribery in any form, including facilitation payments to speed up routine "
        "procedures. Report any request for such a payment to Compliance immediately.")
    d.h("8. Speaking up")
    d.p("If you suspect a breach of this policy or the law, raise it with your manager, Compliance, or through the "
        "whistleblowing channel at speakup.rahmqvist.example. The channel is run by an external partner and you can "
        "report anonymously. You will receive an acknowledgement within seven (7) days and feedback within three (3) "
        "months. Retaliation against anyone who speaks up in good faith is forbidden and is itself a breach of this "
        "policy.")
    d.h("Summary of the rules")
    d.table(["Topic", "Rule"], [
        ["Gifts received or given", "Up to EUR 50 per giver per year; never cash or gift cards"],
        ["Gift register", "Every gift over EUR 25, within 5 working days"],
        ["During a tender", "Nothing at all from participants"],
        ["Public officials", "Branded items under EUR 10 only"],
        ["Business meals", "Up to EUR 120 per person"],
        ["Conflict of interest", "Declare in the HR portal within 30 days"],
        ["Side jobs", "Manager's written approval in advance"],
        ["Whistleblowing", "Anonymous; acknowledged within 7 days, feedback within 3 months"],
    ], [0.32, 0.68])
    d.h("Frequently asked questions")
    d.qa([
        ("A supplier sent me a bottle of wine worth about EUR 40 at Christmas. Can I keep it?",
         "Yes, it is under EUR 50 - but register it in the gift register, because it is worth more than EUR 25. "
         "If that supplier is in an open tender, return it."),
        ("A client offered me two concert tickets but will not be there. Can I accept?",
         "Without the host it counts as a gift, so the EUR 50 limit applies. Tickets worth more must be declined."),
        ("My partner works for one of our suppliers. Is that a problem?",
         "Not in itself, but declare it in the HR portal within 30 days so that you are not the one deciding on "
         "that supplier."),
        ("Can I drive for a ride-hailing service at weekends?",
         "Possibly. A side job needs your manager's written approval in advance, and must not affect your work."),
        ("How do I report something anonymously?",
         "Use the whistleblowing channel at speakup.rahmqvist.example. It is run by an external partner."),
    ])
    d.h("9. Questions")
    d.p("Questions about this policy go to Legal & Compliance at compliance@rahmqvist.example.")
    d.save(out / "code-of-conduct-gifts-and-conflicts.pdf")


def learning(out: Path) -> None:
    d = start("Learning and Development Policy", "HR-12", "2026.1",
              "Policy HR-12 · Version 2026.1 · Effective 1 January 2026 · Owner: People & Culture · Rahmqvist Group")
    d.h("1. Purpose")
    d.p("We want everyone at Rahmqvist to keep growing. This policy explains the time and money available for "
        "learning, what it can be used for, and how to apply.")
    d.h("2. Your learning budget")
    d.p("Every employee has a personal learning budget of EUR 1,500 per calendar year. Part-time employees receive a "
        "share in proportion to their working hours. Employees who join during the year receive one twelfth of the "
        "budget for each month remaining, counting the month they start. Unused budget does not carry over to the "
        "next year.")
    d.h("3. Learning days")
    d.p("In addition to the budget, you have five (5) paid learning days per calendar year, to be agreed with your "
        "manager in advance. Learning days can be used for courses, conferences or structured self-study.")
    d.h("4. What the budget covers")
    d.bullets([
        "Courses, workshops and online learning platforms.",
        "Certification exams, including preparation material.",
        "Conference tickets. Travel to conferences follows the Travel and Expense Policy (FIN-03).",
        "Books and audiobooks related to your work, up to EUR 150 per year within the budget.",
        "Language courses in a language used in your work.",
    ])
    d.h("5. How to apply")
    d.p("Submit a request in the learning portal at least three (3) weeks before the start date. Your manager can "
        "approve anything within your budget. Requests above your remaining budget need your department head's "
        "approval as well.")
    d.h("6. Certifications and exams")
    d.p("The company pays the fee for the first attempt at a certification exam, whatever the result. A retake is "
        "paid once, after you pass it - so a failed retake is at your own cost.")
    d.h("7. Paying back costs if you leave")
    d.p("For a single learning activity that costs the company more than EUR 3,000, you sign a payback agreement. "
        "If you resign within 12 months of completing it, you repay one twelfth of the cost for each month remaining. "
        "Nothing is repaid if the company ends your employment.")
    d.h("8. Learning at Rahmqvist")
    d.p("Rahmqvist Academy offers free internal courses, a mentoring programme and monthly lunch-and-learn sessions. "
        "These do not use your budget or your learning days.")
    d.h("Summary of the rules")
    d.table(["Topic", "Rule"], [
        ["Learning budget", "EUR 1,500 per calendar year; pro rata for part-time and new joiners"],
        ["Carry-over", "None"],
        ["Learning days", "5 paid days per year, agreed in advance"],
        ["Books", "Up to EUR 150 per year, within the budget"],
        ["Applying", "Learning portal, at least 3 weeks before"],
        ["Exams", "First attempt paid; one retake paid if passed"],
        ["Payback", "Activities over EUR 3,000: repay pro rata if you resign within 12 months"],
    ], [0.3, 0.7])
    d.h("Frequently asked questions")
    d.qa([
        ("I joined in July. How much budget do I have this year?",
         "Six twelfths, counting July: EUR 750."),
        ("Can I save this year's budget for an expensive course next year?",
         "No, unused budget does not carry over. For a course costing more than your budget, ask your department head."),
        ("Do books count?",
         "Yes, books and audiobooks related to your work, up to EUR 150 per year within your budget."),
        ("I failed my certification exam. Do I pay for it?",
         "No, the first attempt is paid whatever the result. A retake is paid once you pass it."),
        ("Do I have to pay back a course if I leave?",
         "Only for an activity costing over EUR 3,000, and only if you resign within 12 months of finishing it - then "
         "one twelfth of the cost for each month remaining."),
    ])
    d.h("9. Questions")
    d.p("Questions about this policy go to People & Culture at learning@rahmqvist.example.")
    d.save(out / "learning-and-development-policy.pdf")


def sick_se(out: Path) -> None:
    d = start("Sick Leave Policy - Sweden", "HR-09-SE", "2026.1",
              "Policy HR-09-SE · Version 2026.1 · Effective 1 March 2026 · Owner: HR Sweden · Rahmqvist Sverige AB")
    d.h("1. Purpose and scope")
    d.p("This policy explains what to do when you are ill, what you are paid and how we support your return to work. "
        "It applies to employees on a Swedish employment contract. Employees in other countries follow the sick leave "
        "policy of their own country.")
    d.h("2. Reporting sickness")
    d.p("Tell your line manager before 09:00 on your first day of sickness, by phone or chat, and register the absence "
        "in the HR portal the same day. Register again in the HR portal on the day you return to work.")
    d.h("3. Sick pay")
    d.bullets([
        "A qualifying deduction (karensavdrag) applies, as Swedish law requires: 20% of your average weekly sick pay.",
        "Days 1-14: the company pays sick pay of 80% of your salary.",
        "From day 15: the Swedish Social Insurance Agency (Försäkringskassan) pays sickness benefit; apply to them directly.",
        "From day 15 to day 90, the company tops up your pay so that you receive 90% of your salary in total.",
    ])
    d.h("4. Medical certificate")
    d.p("A medical certificate is needed from day eight (8) of a sickness period, counting calendar days including "
        "weekends. Upload it in the HR portal. After six (6) or more periods of sickness in 12 months, HR may, after a "
        "meeting with you, ask for a certificate from the first day.")
    d.h("5. Caring for a sick child (VAB)")
    d.p("If you stay home to care for a sick child, tell your manager before 09:00 and register VAB in the HR portal. "
        "Temporary parental benefit is paid by Försäkringskassan, so apply to them as well. The company does not pay "
        "salary for VAB days.")
    d.h("6. Health care")
    d.p("The occupational health service (företagshälsovård) is free for all employees. The company's health "
        "insurance gives access to a specialist within seven (7) days and covers up to ten (10) physiotherapy visits "
        "a year.")
    d.h("7. Getting back to work")
    d.p("For an absence longer than four (4) weeks, your manager will draw up a return-to-work plan with you within "
        "30 days, together with the occupational health service if needed.")
    d.h("8. Sick during your vacation")
    d.p("If you fall ill during your vacation, you can have those days counted as sick days instead, and keep the "
        "vacation days. Report the sickness to your manager on the first day, and provide a medical certificate from "
        "the first day of sickness.")
    d.h("Summary of the rules")
    d.table(["Topic", "Rule in Sweden"], [
        ["Report sickness", "To your manager before 09:00, and in the HR portal the same day"],
        ["First day", "Qualifying deduction (karensavdrag) of 20% of average weekly sick pay"],
        ["Days 1-14", "80% of salary, paid by the company"],
        ["From day 15", "Försäkringskassan; company tops up to 90% until day 90"],
        ["Medical certificate", "From day 8 (calendar days)"],
        ["Sick child (VAB)", "Tell your manager before 09:00; benefit from Försäkringskassan"],
        ["Physiotherapy", "Up to 10 visits a year through the health insurance"],
    ], [0.3, 0.7])
    d.h("Frequently asked questions")
    d.qa([
        ("I woke up ill. Who do I tell?",
         "Your line manager, before 09:00, and register the absence in the HR portal the same day."),
        ("Do I get paid on my first day of sickness?",
         "Yes, but with the qualifying deduction (karensavdrag) of 20% of your average weekly sick pay."),
        ("When do I need a doctor's note?",
         "From day eight of the sickness period, counting weekends. Upload it in the HR portal."),
        ("My child is ill. What do I do?",
         "Tell your manager before 09:00, register VAB in the HR portal and apply to Försäkringskassan for the benefit."),
        ("I got ill on vacation. Do I lose those vacation days?",
         "No, if you report it on the first day and provide a medical certificate from the first day of sickness."),
    ])
    d.h("9. Questions")
    d.p("Questions about this policy go to HR Sweden at hr.se@rahmqvist.example.")
    d.save(out / "sick-leave-policy-sweden.pdf")


def sick_no(out: Path) -> None:
    d = start("Sick Leave Policy - Norway", "HR-09-NO", "2026.1",
              "Policy HR-09-NO · Version 2026.1 · Effective 1 March 2026 · Owner: HR Norway · Rahmqvist Norge AS")
    d.h("1. Purpose and scope")
    d.p("This policy explains what to do when you are ill, what you are paid and how we support your return to work. "
        "It applies to employees on a Norwegian employment contract. Employees in other countries follow the sick "
        "leave policy of their own country.")
    d.h("2. Reporting sickness")
    d.p("Tell your line manager before 09:00 on your first day of sickness, and register the absence in the HR portal "
        "the same day. Register again in the HR portal on the day you return to work.")
    d.h("3. Self-certification (egenmelding)")
    d.p("Rahmqvist Norge AS has an extended self-certification arrangement. You can self-certify for up to eight (8) "
        "calendar days at a time, and up to 24 days in total over any 12 months. You can use self-certification once "
        "you have worked for the company for two (2) months; before that, a sick note from a doctor (sykmelding) is "
        "needed from the first day.")
    d.h("4. Sick note (sykmelding)")
    d.p("For sickness lasting longer than eight days, or when your self-certification days are used up, you need a "
        "sick note from a doctor. Your doctor sends it electronically, so you only need to approve it.")
    d.h("5. Sick pay")
    d.bullets([
        "There is no waiting day: you receive full salary from the first day of sickness.",
        "Days 1-16: the company pays sick pay (the employer period, arbeidsgiverperioden).",
        "From day 17: NAV pays sickness benefit, up to 6G.",
        "The company tops up the difference so that you keep your full salary for up to 12 months.",
    ])
    d.h("6. Caring for a sick child (omsorgsdager)")
    d.p("You have ten (10) care days per calendar year for a sick child, or 15 if you have three or more children. "
        "Tell your manager before 09:00 and register the care day in the HR portal. Care days are paid as normal "
        "salary.")
    d.h("7. Health care")
    d.p("The occupational health service (bedriftshelsetjeneste) is free for all employees, and the company's health "
        "insurance gives access to a specialist within seven (7) days.")
    d.h("8. Follow-up during sick leave")
    d.p("Your manager will draw up a follow-up plan (oppfølgingsplan) with you within four (4) weeks of sick leave, "
        "and hold a dialogue meeting within seven (7) weeks, as Norwegian law requires.")
    d.h("9. Sick during your vacation")
    d.p("If you are ill during your vacation and have a sick note from a doctor, you can postpone the vacation days "
        "you lose. Apply to HR as soon as you are back.")
    d.h("Summary of the rules")
    d.table(["Topic", "Rule in Norway"], [
        ["Report sickness", "To your manager before 09:00, and in the HR portal the same day"],
        ["Self-certification", "Up to 8 calendar days at a time, 24 days in 12 months; after 2 months' employment"],
        ["Sick note", "From day 9, or when self-certification is used up"],
        ["Waiting day", "None - full salary from day 1"],
        ["Days 1-16", "Paid by the company"],
        ["From day 17", "NAV pays up to 6G; company tops up to full salary for up to 12 months"],
        ["Sick child", "10 care days a year (15 with three or more children)"],
    ], [0.3, 0.7])
    d.h("Frequently asked questions")
    d.qa([
        ("How many days can I self-certify?",
         "Up to eight calendar days at a time, and 24 days in total over 12 months."),
        ("Is there an unpaid waiting day in Norway?",
         "No. You receive full salary from the first day of sickness."),
        ("I started three weeks ago. Can I self-certify?",
         "Not yet. Self-certification is available after two months' employment; until then you need a sick note."),
        ("My child is ill. How many days can I stay home?",
         "Ten care days per calendar year, or 15 if you have three or more children, paid as normal salary."),
    ])
    d.h("10. Questions")
    d.p("Questions about this policy go to HR Norway at hr.no@rahmqvist.example.")
    d.save(out / "sick-leave-policy-norway.pdf")


def friskvard(out: Path) -> None:
    d = start("Friskvårdspolicy - Sverige", "HR-15-SE", "2026.1",
              "Policy HR-15-SE · Version 2026.1 · Gäller från 1 januari 2026 · Ägare: HR Sverige · "
              "Rahmqvist Sverige AB",
              banner=SV_BANNER, footer_word="TESTDOKUMENT - fiktivt innehåll")
    d.h("1. Syfte")
    d.p("Rahmqvist vill att alla medarbetare ska må bra och orka i arbetet. Policyn beskriver vilket stöd du får "
        "för motion och friskvård, och hur du använder det.")
    d.h("2. Vem som omfattas")
    d.p("Policyn gäller alla tillsvidareanställda i Rahmqvist Sverige AB och visstidsanställda med en anställning på "
        "minst sex (6) månader. Deltidsanställda får bidrag i proportion till sin sysselsättningsgrad.")
    d.h("3. Friskvårdsbidrag")
    d.p("Du får ett friskvårdsbidrag på 3 000 kronor per kalenderår. Bidraget är skattefritt enligt Skatteverkets "
        "regler. Börjar du under året får du en tolftedel av beloppet för varje återstående månad, inklusive den "
        "månad du börjar. Outnyttjat bidrag kan inte sparas till nästa år.")
    d.h("4. Vad bidraget räcker till")
    d.p("Bidraget gäller motion och friskvård av enklare slag, till exempel:")
    d.bullets([
        "gymkort, gruppträning och simhall;",
        "racketsporter som padel, tennis och badminton;",
        "yoga, pilates och löpkurser;",
        "kostrådgivning och stresshantering hos godkänd utövare.",
    ])
    d.p("Bidraget gäller inte utrustning eller träningskläder, golf (greenfee och medlemskap), ridning, eller "
        "medlemsavgifter som inte avser motion.")
    d.h("5. Friskvårdstimme")
    d.p("Du får använda en (1) timme per vecka av arbetstiden till motion, efter överenskommelse med din chef. "
        "Timmen kan inte sparas eller flyttas till en annan vecka.")
    d.h("6. Så får du bidraget")
    d.p("Ladda upp kvittot i HR-portalen senast den 15 december samma år. Kvittot ska visa ditt namn, vilken "
        "aktivitet det gäller, datum och belopp. Bidraget betalas ut med nästa lön efter att kvittot har godkänts.")
    d.h("7. Hälsoundersökning")
    d.p("Alla medarbetare erbjuds en kostnadsfri hälsoundersökning vartannat år via företagshälsovården.")
    d.h("Sammanfattning")
    d.table(["Område", "Regel"], [
        ["Friskvårdsbidrag", "3 000 kr per kalenderår, skattefritt"],
        ["Nyanställd", "En tolftedel per återstående månad, inklusive startmånaden"],
        ["Sparande", "Bidraget och friskvårdstimmen kan inte sparas"],
        ["Friskvårdstimme", "1 timme per vecka, i samråd med chefen"],
        ["Kvitton", "I HR-portalen senast 15 december"],
        ["Hälsoundersökning", "Vartannat år, kostnadsfri"],
    ], [0.3, 0.7])
    d.h("Vanliga frågor")
    d.qa([
        ("Kan jag använda friskvårdsbidraget till padel?",
         "Ja, racketsporter som padel, tennis och badminton ingår."),
        ("Täcker bidraget nya löparskor?",
         "Nej, utrustning och träningskläder ingår inte."),
        ("Jag började i juli. Hur mycket bidrag får jag i år?",
         "Sex tolftedelar, eftersom juli räknas med: 1 500 kronor."),
        ("Kan jag spara friskvårdstimmen till nästa vecka?",
         "Nej, timmen gäller den vecka den avser och kan inte sparas."),
        ("När måste kvittot vara inne?",
         "Senast den 15 december samma år, i HR-portalen."),
    ], q_prefix="F:", a_prefix="S:")
    d.h("8. Frågor")
    d.p("Frågor om policyn skickas till HR Sverige på hr.se@rahmqvist.example.")
    d.save(out / "friskvardspolicy-sverige.pdf", page_label="Sida {n} av {total}")


if __name__ == "__main__":
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    for build in (travel, travel_se, it_policy, conduct, learning, sick_se, sick_no, friskvard):
        build(out)
    print("written to", out)
