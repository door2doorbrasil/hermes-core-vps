from cron.blueprint_catalog import (
    CATALOG,
    blueprint_catalog_entry,
    fill_blueprint,
    get_blueprint,
)
from cron.jobs import parse_schedule


def test_sales_blueprints_are_present_with_fixed_day_intervals():
    buyer = get_blueprint("buyer-outreach-30d")
    secom = get_blueprint("secom-buyer-report-request")

    assert buyer is not None
    assert secom is not None
    assert buyer.schedule_template == "every 30d"
    assert secom.schedule_template == "every 45d"


def test_sales_blueprints_humanize_to_days():
    buyer = blueprint_catalog_entry(get_blueprint("buyer-outreach-30d"))
    secom = blueprint_catalog_entry(get_blueprint("secom-buyer-report-request"))

    assert buyer["scheduleHuman"] == "every 30 days"
    assert secom["scheduleHuman"] == "every 45 days"


def test_sales_blueprints_translate_to_job_specs():
    buyer = get_blueprint("buyer-outreach-30d")
    secom = get_blueprint("secom-buyer-report-request")

    buyer_spec = fill_blueprint(buyer, {})
    secom_spec = fill_blueprint(secom, {})

    assert buyer_spec["schedule"] == "every 30d"
    assert secom_spec["schedule"] == "every 45d"
    assert buyer_spec["skills"] == ["hermes-mail-commercial-operations", "hermes-vendas"]
    assert secom_spec["skills"] == ["hermes-mail-commercial-operations", "hermes-vendas"]


def test_interval_schedules_display_in_days():
    assert parse_schedule("every 30d")["display"] == "every 30d"
    assert parse_schedule("every 45d")["display"] == "every 45d"
