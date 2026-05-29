import os
import unittest

from ecom_parsers import (
    basket_id_from_task,
    exact_count_message,
    exact_quantity_message,
    parse_availability_task,
    parse_constraints,
    payment_id_from_task,
    requested_count_kind,
    select_store,
    store_name_alias,
)


class ConstraintParserTest(unittest.TestCase):
    def test_text_and_numeric_constraints(self):
        parsed = parse_constraints(
            "ip rating IP44, piece count 8 pcs, luminous flux 800 lm, stackable yes"
        )

        by_key = {item.key: item for item in parsed}
        self.assertEqual(by_key["ip_rating"].value_text, "IP44")
        self.assertEqual(by_key["piece_count"].value_number, 8.0)
        self.assertEqual(by_key["lumen"].value_number, 800.0)
        self.assertEqual(by_key["stackable"].value_text, "yes")

    def test_special_volume_and_length_units(self):
        parsed = parse_constraints("volume 500 ml and length 5 m")

        by_key = {item.key: item for item in parsed}
        self.assertEqual(by_key["volume_ml"].value_number, 500.0)
        self.assertEqual(by_key["length_m"].value_number, 5.0)

    def test_constraint_labels_accept_separator_variants(self):
        parsed = parse_constraints(
            "ip-rating IP44, color_family Blue, piece-count 8 pieces, "
            "luminous_flux 800 lm, cable_section 2.5 mm²"
        )

        by_key = {item.key: item for item in parsed}
        self.assertEqual(by_key["ip_rating"].value_text, "IP44")
        self.assertEqual(by_key["color_family"].value_text, "Blue")
        self.assertEqual(by_key["piece_count"].value_number, 8.0)
        self.assertEqual(by_key["lumen"].value_number, 800.0)
        self.assertEqual(by_key["cable_section_mm2"].value_number, 2.5)

    def test_colour_temperature_numeric_constraint(self):
        parsed = parse_constraints("colour temperature 2700 K and color temperature 4000 K")

        values = [item.value_number for item in parsed if item.key == "color_temperature_k"]
        self.assertEqual(values, [2700.0, 4000.0])

    def test_text_constraint_strips_catalogue_suffix(self):
        parsed = parse_constraints("color family Purple in the catalogue")

        self.assertEqual(parsed[0].key, "color_family")
        self.assertEqual(parsed[0].value_text, "Purple")


class TaskParserTest(unittest.TestCase):
    def test_ids_from_task_text(self):
        self.assertEqual(basket_id_from_task("check out basket-012 now"), "basket_012")
        self.assertEqual(payment_id_from_task("recover pay 044"), "pay_044")

    def test_requested_count_kind(self):
        self.assertEqual(
            requested_count_kind("How many catalogue products are Screwdriver and Hex Key Set?"),
            "Screwdriver and Hex Key Set",
        )
        self.assertEqual(
            requested_count_kind(
                "Catalogue count report, how many products are Drill Bit Set?"
            ),
            "Drill Bit Set",
        )
        self.assertEqual(
            requested_count_kind("How many products are Pipe Fitting in catalogue?"),
            "Pipe Fitting",
        )
        self.assertEqual(
            requested_count_kind(
                "For the catalogue count report, how many products are Work Trousers Answer in exactly format \"%d\"."
            ),
            "Work Trousers",
        )

    def test_exact_answer_formats(self):
        self.assertEqual(
            exact_count_message('Answer in exactly format "<COUNT:%d>"', 12),
            "<COUNT:12>",
        )
        self.assertEqual(
            exact_quantity_message('Answer exactly as "[QTY:%d]"', 9),
            "[QTY:9]",
        )
        self.assertEqual(
            exact_count_message("Answer format: `<COUNT:NUMBER>`.", 7),
            "<COUNT:7>",
        )
        self.assertEqual(
            exact_count_message("Answer format: `<total:%VALUE%>`.", 7),
            "<total:7>",
        )
        self.assertEqual(
            exact_count_message('answer pattern: "<QTY:%VALUE%>" (no quotes)', 7),
            "<QTY:7>",
        )
        self.assertEqual(
            exact_count_message('answer pattern: "<QTY: the_actual_number>" (no quotes)', 7),
            "<QTY: 7>",
        )
        self.assertEqual(
            exact_count_message(r'answer pattern: "count\tNUMBER" (no quotes)', 7),
            "count\t7",
        )

    def test_availability_task(self):
        parsed = parse_availability_task(
            "How many of these products have at least 3 items available in the "
            "west-side Vienna today: the Trap from Gardena in the Gardena Aqua "
            "GARD 1AA Trap line that has trap type mole, the Lamp from Philips "
            "in the Philips Bright PHI 2BB Lamp line that has luminous flux 800 lm? "
            "Answer exactly."
        )

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.threshold, 3)
        self.assertEqual(parsed.store_phrase, "west-side Vienna")
        self.assertEqual(parsed.comparator, "gte")
        self.assertEqual(len(parsed.products), 2)
        self.assertEqual(parsed.products[0].kind_name, "Trap")
        self.assertEqual(parsed.products[0].brand, "Gardena")
        self.assertEqual(parsed.products[0].constraints[0].key, "trap_type")
        self.assertEqual(parsed.products[1].constraints[0].key, "lumen")
        self.assertEqual(parsed.products[1].constraints[0].value_number, 800.0)

    def test_availability_task_without_question_mark(self):
        parsed = parse_availability_task(
            "How many of these products have at least 3 items available in the "
            "north Graz PowerTool branch today: the Tool Box and Bag from Festool "
            "in the Festool Stackable SYS 3JJ-9LM Tool Box and Bag line that has "
            "storage type tool bag Answer in exactly format \"%d\"."
        )

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.threshold, 3)
        self.assertEqual(parsed.store_phrase, "north Graz PowerTool branch")
        self.assertEqual(parsed.comparator, "gte")

    def test_or_more_ready_availability_task(self):
        parsed = parse_availability_task(
            "How many of these products have 3 or more ready in the north Graz "
            "PowerTool branch today: the Tool Box and Bag from Festool in the "
            "Festool Stackable SYS 3JJ-9LM Tool Box and Bag line that has storage "
            "type tool bag? Answer in exactly format \"%d\"."
        )

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.threshold, 3)
        self.assertEqual(parsed.store_phrase, "north Graz PowerTool branch")
        self.assertEqual(parsed.comparator, "gte")

    def test_below_availability_task(self):
        parsed = parse_availability_task(
            "Could you please tell me how many from this list are below 3 available "
            "today at the Lend district PowerTool store: the Valve from Rothenberger "
            "in the Rothenberger Rocut Rofrost 2I6-9CE Valve and Connector line that "
            "has connector type shower hose and diameter 32 mm, the Pipe Fitting from "
            "Wavin in the Wavin Compact Tigris 2HC-AJD Pipe Fitting line that has "
            "fitting type pipe fitting and diameter 25 mm? Answer in exactly format "
            "\"count : %d\" (no quotes)"
        )

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.threshold, 3)
        self.assertEqual(parsed.store_phrase, "Lend district PowerTool store")
        self.assertEqual(parsed.comparator, "lt")
        self.assertEqual(len(parsed.products), 2)
        self.assertEqual(parsed.products[0].constraints[0].key, "connector_type")
        self.assertEqual(parsed.products[1].constraints[1].key, "diameter_mm")

    def test_fewer_than_available_today_at_store_task(self):
        parsed = parse_availability_task(
            "Could you tell me how many from this list are fewer than 3 available "
            "today at the Lend district PowerTool store: the Valve from Rothenberger "
            "in the Rothenberger Rocut Rofrost 2I6-9CE Valve and Connector line that "
            "has connector type shower hose and diameter 32 mm Answer in exactly "
            "format \"<COUNT:%d>\"."
        )

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.threshold, 3)
        self.assertEqual(parsed.store_phrase, "Lend district PowerTool store")
        self.assertEqual(parsed.comparator, "lt")

    def test_ready_availability_task(self):
        parsed = parse_availability_task(
            "hey can u check the PowerTool shop by Praterstern in Vienna today and "
            "tell me how many of these have 2 or more ready: the Drain Trap and "
            "Siphon from Wavin in the Wavin Professional Tigris 31G-M1A Drain Trap "
            "and Siphon line that has trap type drain trap and diameter 25 mm, the "
            "Work Trousers from Carhartt in the Carhartt Stretch Rugged 3EY-11K Work "
            "Trousers line that has color family Yellow and size XS? Answer in "
            "exactly format \"count : %d\" (no quotes)"
        )

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.threshold, 2)
        self.assertEqual(parsed.store_phrase, "PowerTool shop by Praterstern in Vienna")
        self.assertEqual(parsed.comparator, "gte")
        self.assertEqual(len(parsed.products), 2)
        self.assertEqual(parsed.products[0].family_name, "Wavin Professional Tigris 31G-M1A Drain Trap and Siphon")
        self.assertEqual(parsed.products[0].constraints[1].key, "diameter_mm")

    def test_store_first_less_than_availability_task(self):
        parsed = parse_availability_task(
            "pls check Brno Veveri hardware store, how many of these have less than "
            "3 available today: the Extension Cable from Schneider Electric in the "
            "Schneider Electric Heavy Duty Merten 214-ZHY Extension Cable line that "
            "has color family White and length 3 m, the Work Jacket from Mascot in "
            "the Mascot Advanced ACC 35W-IIS Work Jacket line that has color family "
            "Black? Answer in exactly format \"<COUNT:%d>\" (no quotes)"
        )

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.threshold, 3)
        self.assertEqual(parsed.store_phrase, "Brno Veveri hardware store")
        self.assertEqual(parsed.comparator, "lt")
        self.assertEqual(parsed.products[0].constraints[0].key, "color_family")
        self.assertEqual(parsed.products[0].constraints[1].key, "length_m")

    def test_fewer_than_items_available_in_store_task(self):
        parsed = parse_availability_task(
            "How many of these products have fewer than 5 items available in the "
            "central Brno PowerTool branch today: the Wiring Device from Legrand in "
            "the Legrand Valena Plexo 2UK-M0J Wiring Device line that has device "
            "type switch, the Work Top from Dickies in the Dickies Fleece Redhawk "
            "MRB-WYE Work Top line that has garment type t-shirt? Answer in exactly "
            "format \"<COUNT:%d>\" (no quotes)"
        )

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.threshold, 5)
        self.assertEqual(parsed.store_phrase, "central Brno PowerTool branch")
        self.assertEqual(parsed.comparator, "lt")
        self.assertEqual(len(parsed.products), 2)

    def test_not_available_today_availability_task(self):
        parsed = parse_availability_task(
            "at the Salzburg Elisabeth-Vorstadt hardware store, how many of these just "
            "are not available today: the Hammer from Irwin in the Irwin Vise-Grip "
            "Vise-Grip 3QR-2ZH Hammer Measuring and Cutting Tool line that has tool "
            "type chisel and length 300 mm, the Cordless Drill Driver from Einhell in "
            "the Einhell Expert GC RPD-J21 Cordless Drill Driver line that has voltage "
            "36 V? Answer in exactly format \"<COUNT:%d>\" (no quotes)"
        )

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.threshold, 1)
        self.assertEqual(parsed.store_phrase, "Salzburg Elisabeth-Vorstadt hardware store")
        self.assertEqual(parsed.comparator, "lt")
        self.assertEqual(parsed.products[0].constraints[0].key, "tool_type")
        self.assertEqual(parsed.products[0].constraints[1].key, "length_mm")

    def test_no_same_day_availability_task(self):
        parsed = parse_availability_task(
            "How many of these products have no same-day availability in Bratislava "
            "Stare Mesto hardware branch today: the Adhesive and Glue from Gorilla "
            "in the Gorilla Crystal Grip 2ZQ-D83 Adhesive and Glue line that has "
            "adhesive type wood glue, the LED Bulb from Philips in the Philips "
            "Professional Hue 3JS-MSN LED Bulb line that has wattage 10 W? Answer "
            "in exactly format \"<COUNT:%d>\" (no quotes)"
        )

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.threshold, 1)
        self.assertEqual(parsed.store_phrase, "Bratislava Stare Mesto hardware branch")
        self.assertEqual(parsed.comparator, "lt")
        self.assertEqual(len(parsed.products), 2)


class StoreAliasTest(unittest.TestCase):
    def test_store_name_alias(self):
        self.assertEqual(
            store_name_alias("Vienna", "I can visit the west-side Vienna branch"),
            "Meidling",
        )
        self.assertEqual(store_name_alias("Vienna", "west"), "Meidling")
        self.assertEqual(store_name_alias("Vienna", "east"), "Praterstern")
        self.assertEqual(store_name_alias("Linz", "hardware"), "Hauptplatz")
        self.assertEqual(store_name_alias("Linz", "branch"), "Hauptplatz")
        self.assertEqual(
            store_name_alias("Graz", "I can visit north Graz today"),
            "Lend",
        )
        self.assertEqual(
            store_name_alias("Graz", "Lend district PowerTool store"),
            "Lend",
        )
        self.assertEqual(store_name_alias("Graz", "I can visit north Graz", "Lend"), "")

    def test_select_store(self):
        rows = [
            {"id": "store_vienna_meidling", "city": "Vienna", "name": "PowerTool Vienna Meidling"},
            {
                "id": "store_vienna_praterstern",
                "city": "Vienna",
                "name": "PowerTool Vienna Praterstern",
            },
            {"id": "store_graz_lend", "city": "Graz", "name": "PowerTool Graz Lend"},
            {
                "id": "store_graz_jakomini",
                "city": "Graz",
                "name": "PowerTool Graz Jakomini",
            },
        ]

        self.assertEqual(select_store(rows, "west-side Vienna")["id"], "store_vienna_meidling")
        self.assertEqual(select_store(rows, "central Vienna")["id"], "store_vienna_praterstern")
        self.assertEqual(select_store(rows, "north Graz")["id"], "store_graz_lend")
        self.assertEqual(select_store(rows, "northern Graz")["id"], "store_graz_lend")
        self.assertEqual(select_store(rows, "Graz north branch")["id"], "store_graz_lend")
        self.assertEqual(select_store(rows, "Lend district PowerTool store")["id"], "store_graz_lend")
        self.assertEqual(select_store(rows, "Lend area hardware branch")["id"], "store_graz_lend")
        self.assertEqual(select_store(rows, "central Graz")["id"], "store_graz_jakomini")

    def test_select_store_does_not_pick_arbitrary_city_match(self):
        rows = [
            {"id": "store_prod_graz_alpha", "city": "Graz", "name": "PowerTool Graz Alpha"},
            {"id": "store_prod_graz_beta", "city": "Graz", "name": "PowerTool Graz Beta"},
        ]

        self.assertIsNone(select_store(rows, "north Graz"))

    def test_select_store_prefers_runtime_metadata_over_config_alias(self):
        rows = [
            {
                "id": "store_graz_lend",
                "city": "Graz",
                "name": "PowerTool Graz Lend",
                "direction": "south",
            },
            {
                "id": "store_prod_graz_alpha",
                "city": "Graz",
                "name": "PowerTool Graz Alpha",
                "direction": "north",
            },
        ]

        self.assertEqual(select_store(rows, "north Graz")["id"], "store_prod_graz_alpha")

    def test_store_aliases_can_be_disabled_for_overfit_probe(self):
        rows = [
            {"id": "store_graz_lend", "city": "Graz", "name": "PowerTool Graz Lend"},
            {
                "id": "store_graz_jakomini",
                "city": "Graz",
                "name": "PowerTool Graz Jakomini",
            },
        ]
        old = os.environ.get("ECOM_DISABLE_STORE_ALIASES")
        os.environ["ECOM_DISABLE_STORE_ALIASES"] = "1"
        try:
            self.assertIsNone(select_store(rows, "north Graz"))
            self.assertEqual(store_name_alias("Graz", "north Graz"), "")
        finally:
            if old is None:
                os.environ.pop("ECOM_DISABLE_STORE_ALIASES", None)
            else:
                os.environ["ECOM_DISABLE_STORE_ALIASES"] = old

    def test_select_store_allows_single_city_candidate(self):
        rows = [
            {"id": "store_prod_graz_only", "city": "Graz", "name": "PowerTool Graz"},
        ]

        self.assertEqual(select_store(rows, "north Graz")["id"], "store_prod_graz_only")


if __name__ == "__main__":
    unittest.main()
