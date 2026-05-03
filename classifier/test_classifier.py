"""
test_classifier.py
Run with: GROQ_API_KEY=your_key python test_classifier.py
"""

from classifier import Classifier

classifier = Classifier(package_prefix="com.app")


def run_test(name, input_text, mode=None, checks=None):
    print("\n" + "=" * 60)
    print(f"{name}")
    print("=" * 60)

    if mode:
        out = classifier.classify(input_text, mode=mode)
    else:
        out = classifier.classify(input_text)

    print(out.summary())

    if checks:
        passed = 0
        failed = 0
        for check_fn in checks:
            try:
                result = check_fn(out)
                assert result is not False
                print("  ✅ Check passed")
                passed += 1
            except (AssertionError, AttributeError, TypeError) as e:
                print(f"  ❌ Check failed: {e}")
                failed += 1
        print(f"  → {passed} passed, {failed} failed")


# ─────────────────────────────────────────────────────────────
# 1. BUSINESS TEST CASES
# ─────────────────────────────────────────────────────────────

run_test(
    "B1 — Simple business input",
    "Save button not working on contact us page for user 567",
    mode="business",
    checks=[
        lambda o: o.mode == "business",
        lambda o: "567" in o.intent.entity_ids,
    ],
)

run_test(
    "B2 — Multiple identifiers",
    "Payment failed for order ORD-9921 for user U-77 on checkout page",
    mode="business",
    checks=[
        lambda o: len(o.intent.entity_ids) >= 2,
    ],
)

run_test(
    "B3 — Business with endpoint",
    "Getting error while calling /api/payment/submit for invoice 8821",
    mode="business",
    checks=[
        lambda o: o.intent.endpoint == "/api/payment/submit",
        lambda o: "8821" in o.intent.entity_ids,
    ],
)

run_test(
    "B4 — Vague input",
    "Application is slow and sometimes crashes",
    mode="business",
    checks=[
        lambda o: o.failure.symptom != "",
        lambda o: o.confidence <= 0.7,
    ],
)

run_test(
    "B5 — Explicit action",
    "User cannot submit the form",
    mode="business",
    checks=[
        lambda o: o.intent.action == "submit",
    ],
)

# ─────────────────────────────────────────────────────────────
# 2. CLEAN DEVELOPER CASES
# ─────────────────────────────────────────────────────────────

run_test(
    "D1 — Basic NPE",
    """java.lang.NullPointerException
    at com.app.service.UserService.get(UserService.java:32)""",
    mode="developer",
    checks=[
        lambda o: o.failure.error_type == "NullPointerException",
        lambda o: o.location.primary_frame is not None and o.location.primary_frame.file_name == "UserService.java",
    ],
)

run_test(
    "D2 — HTTP + exception",
    """ERROR 404 on /api/user/12
java.lang.IllegalArgumentException: Invalid ID
    at com.app.controller.UserController.get(UserController.java:21)""",
    mode="developer",
    checks=[
        lambda o: o.failure.http_status == 404,
        lambda o: o.intent.endpoint == "/api/user/12",
    ],
)

# ─────────────────────────────────────────────────────────────
# 3. STACKOVERFLOW-LIKE TRACES
# ─────────────────────────────────────────────────────────────

run_test(
    "SO1 — Spring heavy stack",
    """org.springframework.web.util.NestedServletException: Request processing failed
    at org.springframework.web.servlet.FrameworkServlet.processRequest(FrameworkServlet.java:1)
    at org.springframework.web.servlet.FrameworkServlet.doPost(FrameworkServlet.java:2)
    at com.app.service.OrderService.create(OrderService.java:88)
    at com.app.controller.OrderController.create(OrderController.java:40)
Caused by: java.lang.NullPointerException: Cannot invoke Order.getId() because order is null""",
    mode="developer",
    checks=[
        lambda o: len(o.evidence.user_frames()) == 2,
        lambda o: o.cause is not None and o.cause.error_type == "NullPointerException",
    ],
)

run_test(
    "SO2 — Hibernate + SQL",
    """org.hibernate.exception.JDBCConnectionException: could not extract ResultSet
    at org.hibernate.loader.Loader.getResultSet(Loader.java:1)
    at com.app.repository.UserRepo.find(UserRepo.java:67)
Caused by: java.sql.SQLException: Connection timeout""",
    mode="developer",
    checks=[
        lambda o: o.cause is not None and o.cause.error_type == "SQLException",
        lambda o: o.location.primary_frame is not None and o.location.primary_frame.file_name == "UserRepo.java",
    ],
)

run_test(
    "SO3 — Recursive stack",
    """java.lang.StackOverflowError
    at com.app.service.RecursiveService.call(RecursiveService.java:10)
    at com.app.service.RecursiveService.call(RecursiveService.java:10)
    at com.app.service.RecursiveService.call(RecursiveService.java:10)""",
    mode="developer",
    checks=[
        lambda o: len(o.evidence.frames) >= 1,
    ],
)

# ─────────────────────────────────────────────────────────────
# 4. MIXED INPUTS
# ─────────────────────────────────────────────────────────────

run_test(
    "M1 — Email + stack",
    """Hi team,

User cannot save trade.

java.lang.NullPointerException
    at com.app.service.TradeService.save(TradeService.java:142)
    at com.app.controller.TradeController.saveTrade(TradeController.java:87)
""",
    checks=[
        lambda o: o.mode == "developer",
        lambda o: o.failure.error_type == "NullPointerException",
    ],
)

run_test(
    "M2 — Business + logs",
    """Checkout failing for order 991

ERROR Payment declined
java.lang.RuntimeException: Gateway timeout""",
    checks=[
        lambda o: "991" in o.intent.entity_ids,
        lambda o: o.failure.error_type == "RuntimeException",
    ],
)

# ─────────────────────────────────────────────────────────────
# 5. NOISY INPUTS
# ─────────────────────────────────────────────────────────────

run_test(
    "N1 — Partial stack",
    """java.lang.NullPointerException
    at com.app.service.UserService.get(""",
    mode="developer",
    checks=[
        lambda o: o.failure.error_type == "NullPointerException",
    ],
)

run_test(
    "N2 — Logs only",
    """2024-01-01 ERROR Payment failed
2024-01-01 ERROR DB timeout""",
    mode="developer",
    checks=[
        lambda o: len(o.evidence.logs) > 0,
    ],
)

run_test(
    "N3 — Garbage input",
    "asdf123 !! ERROR?? maybe something broke idk",
    mode="business",
    checks=[
        lambda o: o.confidence <= 0.6,
    ],
)

# ─────────────────────────────────────────────────────────────
# 6. EDGE CASES
# ─────────────────────────────────────────────────────────────

run_test(
    "E1 — Endpoint not action",
    "Error on /api/deleteUser",
    mode="business",
    checks=[
        lambda o: o.intent.action is None,
    ],
)

run_test(
    "E2 — Multiple causes",
    """java.lang.RuntimeException
Caused by: java.io.IOException
Caused by: java.sql.SQLException: Connection failed""",
    mode="developer",
    checks=[
        lambda o: o.cause is not None,
    ],
)

run_test(
    "E3 — No user code frames",
    """java.lang.NullPointerException
    at org.springframework.web.servlet.FrameworkServlet.doPost(FrameworkServlet.java:1)
    at org.hibernate.loader.Loader.getResultSet(Loader.java:2)""",
    mode="developer",
    checks=[
        lambda o: o.location.primary_frame is None,
    ],
)

print("\n🚀 ALL TESTS EXECUTED\n")
