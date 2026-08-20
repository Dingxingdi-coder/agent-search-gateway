from io import StringIO

from agent_search_gateway.doctor import (
    DoctorCheck,
    DoctorReport,
    DoctorStatus,
    render_doctor,
)


def test_doctor_report_exit_code_and_rendering_are_deterministic() -> None:
    healthy = DoctorReport(
        (
            DoctorCheck(DoctorStatus.OK, "configuration valid"),
            DoctorCheck(DoctorStatus.INFO, "daemon not running"),
        )
    )
    stream = StringIO()
    render_doctor(healthy, stream)

    assert healthy.exit_code == 0
    assert stream.getvalue() == "[ok] configuration valid\n[info] daemon not running\n"

    failing = DoctorReport(
        (
            DoctorCheck(DoctorStatus.OK, "first"),
            DoctorCheck(DoctorStatus.FAIL, "broken\nwith\ttabs"),
        )
    )
    stream = StringIO()
    render_doctor(failing, stream)

    assert failing.exit_code == 1
    assert stream.getvalue() == "[ok] first\n[fail] broken with tabs\n"
    assert len(stream.getvalue().splitlines()) == 2
