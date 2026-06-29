from __future__ import annotations

import json
from pathlib import Path

from hermes_compras_state import ComprasDB


def test_compras_db_bootstraps_required_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "hermes_compras.db"
    with ComprasDB(db_path=db_path) as db:
        expected = {
            "audit_logs",
            "entity_field_sources",
            "freight_market_intelligence",
            "hermes_decision_recommendations",
            "logistics_news_alerts",
            "products",
            "profit_margin_rules",
            "purchase_timing_analysis",
            "rfq_batches",
            "rfq_candidates",
            "rfq_email_attachments",
            "rfq_email_logs",
            "rfq_email_threads",
            "rfq_followup_logs",
            "rfq_inbound_emails",
            "rfq_messages",
            "rfq_recipients",
            "sale_price_calculations",
            "schema_version",
            "secom_offices",
            "supplier_contacts",
            "supplier_product_matches",
            "supplier_quote_commercial_terms",
            "supplier_quote_specifications",
            "supplier_quotes",
            "suppliers",
            "user_decision_logs",
        }
        assert expected.issubset(set(db.list_tables()))
        assert db.table_exists("suppliers")
        assert db.table_exists("rfq_candidates")
        assert db.table_exists("secom_offices")
        assert db.fetchone("SELECT version FROM schema_version LIMIT 1").data["version"] == 5


def test_compras_db_approval_helpers_store_state(tmp_path: Path) -> None:
    db_path = tmp_path / "hermes_compras.db"
    with ComprasDB(db_path=db_path) as db:
        product_id = db.insert_product(name="AMR Robot", created_at=1.0, updated_at=1.0)
        batch_id = db.insert_rfq_batch(batch_code="RFQ-TEST-001", product_id=product_id, requested_by="tester", created_at=1.0, updated_at=1.0)
        candidate_ids = db.store_rfq_candidates(
            batch_id,
            [
                {
                    "legal_name": "Alpha Robotics",
                    "country": "CN",
                    "city": "Shenzhen",
                    "website": "https://alpha.example",
                    "source_url": "https://alpha.example",
                    "manufacturer_flag": True,
                },
                {
                    "legal_name": "Beta Trading",
                    "country": "US",
                    "city": "Miami",
                    "website": "https://beta.example",
                    "source_url": "https://beta.example",
                    "trading_company_flag": True,
                },
            ],
            created_at=1.0,
            updated_at=1.0,
        )
        assert len(candidate_ids) == 2
        result = db.approve_rfq_suppliers(
            rfq_batch_id=batch_id,
            approved_supplier_candidates=[{"legal_name": "Alpha Robotics", "country": "CN", "city": "Shenzhen", "website": "https://alpha.example", "source_url": "https://alpha.example"}],
            rejected_supplier_candidates=[{"legal_name": "Beta Trading", "country": "US", "city": "Miami", "website": "https://beta.example", "source_url": "https://beta.example"}],
            approved_by="tester",
            approval_notes="partial approval",
            authorize_email_send=True,
            dry_run=True,
            created_at=1.0,
        )
        assert result["approved_count"] == 1
        assert result["rejected_count"] == 1
        assert result["email_authorized"] is True
        assert result["dry_run"] is True
        assert result["audit_log_ids"]
        assert result["decision_log_ids"]
        assert result["email_log_ids"]
        assert db.fetchone("SELECT status FROM rfq_batches WHERE id = ?", (batch_id,)).data["status"] in {"authorized", "approved_without_email"}
        assert db.fetchone("SELECT COUNT(*) AS c FROM user_decision_logs WHERE rfq_batch_id = ?", (batch_id,)).data["c"] >= 2
        assert db.fetchone("SELECT COUNT(*) AS c FROM audit_logs WHERE entity_type IN ('rfq_candidate', 'supplier')", ()).data["c"] >= 2
        assert db.list_rfq_candidates(batch_id)


def test_compras_db_lists_rfq_batches_with_counters(tmp_path: Path) -> None:
    db_path = tmp_path / "hermes_compras.db"
    with ComprasDB(db_path=db_path) as db:
        product_id = db.insert_product(name="AMR Robot", created_at=1.0, updated_at=1.0)
        batch_id = db.insert_rfq_batch(batch_code="RFQ-LIST-001", product_id=product_id, requested_by="tester", created_at=1.0, updated_at=1.0)
        db.store_rfq_candidates(
            batch_id,
            [
                {
                    "legal_name": "Alpha Robotics",
                    "country": "CN",
                    "city": "Shenzhen",
                    "website": "https://alpha.example",
                    "source_url": "https://alpha.example",
                    "manufacturer_flag": True,
                },
            ],
            created_at=1.0,
            updated_at=1.0,
        )
        rows = db.list_rfq_batches()
        assert rows
        batch = rows[0].data
        assert batch["id"] == batch_id
        assert batch["product_name"] == "AMR Robot"
        assert batch["candidate_count"] == 1
        assert batch["quote_count"] == 0
        assert batch["inbound_count"] == 0


def test_compras_db_ranks_candidates_before_store(tmp_path: Path) -> None:
    db_path = tmp_path / "hermes_compras.db"
    with ComprasDB(db_path=db_path) as db:
        product_id = db.insert_product(name="AMR Robot", created_at=1.0, updated_at=1.0)
        batch_id = db.insert_rfq_batch(batch_code="RFQ-RANK-001", product_id=product_id, requested_by="tester", created_at=1.0, updated_at=1.0)
        db.store_rfq_candidates(
            batch_id,
            [
                {
                    "legal_name": "Beta Trading",
                    "country": "US",
                    "city": "Miami",
                    "website": "https://beta.example",
                    "source_url": "https://beta.example",
                    "trading_company_flag": True,
                    "source_type": "marketplace",
                    "verified_status": "unverified",
                    "data_quality_status": "pending_validation",
                },
                {
                    "legal_name": "Alpha Robotics",
                    "country": "CN",
                    "city": "Shenzhen",
                    "website": "https://alpha.example",
                    "source_url": "https://alpha.example",
                    "general_email": "sales@alpha.example",
                    "sales_email": "rfq@alpha.example",
                    "phone": "+86 123456",
                    "manufacturer_flag": True,
                    "source_type": "official_site",
                    "verified_status": "verified",
                    "data_quality_status": "complete",
                },
            ],
            created_at=1.0,
            updated_at=1.0,
        )
        rows = db.list_rfq_candidates(batch_id)
        assert len(rows) == 2
        first = rows[0].data
        second = rows[1].data
        assert first["legal_name"] == "Alpha Robotics"
        assert second["legal_name"] == "Beta Trading"
        assert first["candidate_payload_json"]
        assert second["candidate_payload_json"]
        assert "qualification_score" in first["candidate_payload_json"]
        alpha_payload = json.loads(first["candidate_payload_json"])
        beta_payload = json.loads(second["candidate_payload_json"])
        assert alpha_payload["qualification_status"] == "approved_for_rfq"
        assert beta_payload["qualification_status"] in {"manual_review_required", "not_qualified"}


def test_compras_db_records_inbound_quotes_and_pricing(tmp_path: Path) -> None:
    db_path = tmp_path / "hermes_compras.db"
    with ComprasDB(db_path=db_path) as db:
        product_id = db.insert_product(name="Sodium Acid Pyrophosphate", created_at=1.0, updated_at=1.0)
        supplier_id = db.insert_supplier_candidate(legal_name="Mupro Food", created_at=1.0, updated_at=1.0, website="https://muprofood.example")
        contact_id = db.upsert_supplier_contact(
            supplier_id=supplier_id,
            name="Hedy",
            role="Sales Manager",
            email="sales@muprofood.example",
            phone="+86 518 8585 1997",
            whatsapp="+86 15298603800",
            language="en",
            is_primary=True,
            source="rfq_inbound_email",
            created_at=1.0,
            updated_at=1.0,
        )
        batch_id = db.insert_rfq_batch(batch_code="RFQ-FLOW-001", product_id=product_id, requested_by="tester", created_at=1.0, updated_at=1.0)
        inbound_email_id = db.record_rfq_inbound_email(
            rfq_batch_id=batch_id,
            supplier_id=supplier_id,
            contact_id=contact_id,
            message_id="<reply-1@example>",
            in_reply_to="<rfq-1@example>",
            email_references="<rfq-1@example>",
            from_email="sales@muprofood.example",
            from_name="Hedy",
            to_email="buyer@example",
            cc=None,
            subject="Re: Quotation",
            received_at=2.0,
            body_text="USD 1345/MT FOB China Main Port",
            body_html=None,
            body_summary="supplier replied with price",
            detected_language="en",
            has_attachments=True,
            attachment_count=2,
            correlation_token="token-1",
            matched_by="message_id",
            matching_confidence=0.95,
            is_direct_reply=True,
            created_at=2.0,
            updated_at=2.0,
        )
        quote_id = db.upsert_supplier_quote(
            rfq_batch_id=batch_id,
            supplier_id=supplier_id,
            contact_id=contact_id,
            product_id=product_id,
            source_type="inbound_email",
            source_email_id=inbound_email_id,
            currency="USD",
            unit_price=1345.0,
            quantity=25.0,
            unit="MT",
            incoterm="FOB",
            payment_terms="30% advance / 70% after shipment",
            packaging="25kg bags",
            raw_response="USD 1345/MT FOB China Main Port",
            status="parsed",
            created_at=3.0,
            updated_at=3.0,
        )
        calc_id = db.calculate_sale_price(
            rfq_batch_id=batch_id,
            supplier_id=supplier_id,
            product_id=product_id,
            quote_id=quote_id,
            margin_type="percentage",
            margin_value=0.20,
            international_freight=100.0,
            insurance=10.0,
            origin_charges=5.0,
            destination_charges=15.0,
            customs_clearance_cost=20.0,
            import_duties_estimated=30.0,
            taxes_estimated=40.0,
            inland_freight=50.0,
            warehouse_cost=60.0,
            financial_cost=25.0,
            other_costs=5.0,
            requires_user_approval=False,
            approved_by="tester",
            approved_at=4.0,
            created_at=4.0,
            updated_at=4.0,
        )
        proposal = db.build_proposal_snapshot(
            rfq_batch_id=batch_id,
            quote_id=quote_id,
            sale_price_calculation_id=calc_id,
            created_at=5.0,
        )

    assert inbound_email_id > 0
    assert contact_id > 0
    assert quote_id > 0
    assert calc_id > 0
    assert proposal["quote"]["unit_price"] == 1345.0
    assert proposal["pricing"]["sale_unit_price"] > proposal["pricing"]["total_landed_cost"]
    assert "mupro food" in proposal["proposal_text"].lower()
    assert "margem" in proposal["proposal_text"].lower()


def test_compras_db_seeds_product_catalog_and_secom_registry(tmp_path: Path) -> None:
    db_path = tmp_path / "hermes_compras.db"
    with ComprasDB(db_path=db_path) as db:
        products = db.list_products(search="Bicarbonato")
        assert products
        product = products[0].data
        assert "sales_brief" in product
        assert product["sales_brief"]["mandatory_specs"]

        secom_countries = db.list_secom_countries()
        assert secom_countries
        assert "Chile" in secom_countries

        office_id = db.upsert_secom_office(
            country="Mexico",
            office_name="SECOM Mexico City",
            city="Mexico City",
            email_primary="secom@example.mx",
            product_focus=["amidos"],
            created_at=1.0,
            updated_at=1.0,
        )
        assert office_id > 0
        recommended = db.recommend_secom_offices_for_product(product_name="Polvilho Azedo", limit=5)
        assert recommended
        assert any(row.data["country"] in {"Chile", "Mexico"} for row in recommended)


def test_compras_prompt_never_exposes_sensitive_data() -> None:
    prompt = Path("/Users/aluizioandreatta/Documents/Polar Sinergy LLC/local-hermes/app/main.py").read_text(encoding="utf-8")
    assert "informação sensível" in prompt or "informacao sensivel" in prompt
    assert "senhas" in prompt
    assert "chaves" in prompt
