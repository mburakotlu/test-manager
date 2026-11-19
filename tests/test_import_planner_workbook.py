from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import openpyxl
from openpyxl.styles import PatternFill

from import_planner_workbook import read_vehicle_tab_history, read_workbook


class WorkbookImportTests(unittest.TestCase):
    def test_reads_vehicle_fields_and_dated_activities(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "calendar.xlsx"
            workbook = openpyxl.Workbook()
            sheet = workbook.active
            sheet.title = "Jahreskalender"
            sheet["B8"] = "Name"
            sheet["C8"] = "Logger"
            sheet["D8"] = "Baureihe"
            sheet["E8"] = "Cluster"
            sheet["F8"] = "Markt"
            sheet["G8"] = "VR-Stand"
            sheet["H8"] = "Verwendungszweck"
            sheet["I8"] = "VIN"
            sheet["J8"] = datetime(2026, 11, 18)
            sheet["K8"] = datetime(2026, 6, 13)
            sheet["L8"] = datetime(2026, 6, 14)
            sheet["O8"] = datetime(2026, 6, 15)
            sheet["P8"] = datetime(2026, 6, 16)
            sheet["Q8"] = datetime(2026, 6, 17)
            sheet["B9"] = "MLBevo"
            sheet["B10"] = "PT1023"
            sheet["C10"] = "Ja"
            sheet["D10"] = "Series 1"
            sheet["E10"] = "CL37"
            sheet["F10"] = "DE"
            sheet["G10"] = "19.0.1"
            sheet["H10"] = "Campaign use"
            sheet["I10"] = "VIN-1"
            sheet["J10"] = "Campaign activity"
            sheet["J10"].fill = PatternFill(fill_type="solid", fgColor="FF12AB34")
            sheet["K10"] = "Yellow Saturday activity"
            sheet["K10"].fill = PatternFill(fill_type="solid", fgColor="FFFFC000")
            sheet["L10"] = "Sunday activity"
            sheet["B11"] = "E³ 1.2"
            sheet["B12"] = "SE0534"
            sheet["D12"] = "H2"
            sheet["J12"] = "Second activity"
            sheet["O12"] = "Three-day merged activity"
            sheet.merge_cells("O12:Q12")
            detail_sheet = workbook.create_sheet("PT1023")
            detail_sheet["A1"] = "Wer?"
            detail_sheet["B1"] = "Was?"
            detail_sheet["C1"] = "Wann?"
            detail_sheet["A2"] = "Tester"
            detail_sheet["B2"] = "Detailed work note"
            detail_sheet["C2"] = datetime(2026, 7, 28)
            workbook.save(path)

            vehicles, bookings = read_workbook(path)
            details = read_vehicle_tab_history(path)

        self.assertEqual(len(vehicles), 2)
        self.assertEqual(vehicles[0]["label"], "PT1023")
        self.assertEqual(vehicles[0]["group_name"], "MLBevo")
        self.assertEqual(vehicles[0]["group_order"], "0")
        self.assertEqual(vehicles[0]["display_order"], "0")
        self.assertEqual(vehicles[0]["logger"], "Yes")
        self.assertEqual(vehicles[0]["series"], "Series 1")
        self.assertEqual(vehicles[0]["purpose"], "Campaign use")
        self.assertEqual(vehicles[1]["label"], "SE0534")
        self.assertEqual(vehicles[1]["group_name"], "E³ 1.2")
        self.assertEqual(vehicles[1]["group_order"], "1")
        self.assertEqual(vehicles[1]["display_order"], "1")
        self.assertEqual(len(bookings), 5)
        self.assertEqual(bookings[0]["vehicle_label"], "PT1023")
        self.assertEqual(bookings[0]["planned_date"], "2026-11-18")
        self.assertEqual(bookings[0]["planned_end_date"], "2026-11-18")
        self.assertEqual(bookings[0]["period"], "KW47-2026")
        self.assertEqual(bookings[0]["activity_color"], "#12AB34")
        self.assertEqual(bookings[1]["planned_date"], "2026-06-13")
        self.assertEqual(bookings[1]["activity"], "Yellow Saturday activity")
        self.assertEqual(bookings[1]["activity_color"], "#FFC000")
        self.assertEqual(bookings[2]["planned_date"], "2026-06-14")
        self.assertEqual(bookings[2]["activity"], "Sunday activity")
        merged_booking = next(booking for booking in bookings if booking["activity"] == "Three-day merged activity")
        self.assertEqual(merged_booking["planned_date"], "2026-06-15")
        self.assertEqual(merged_booking["planned_end_date"], "2026-06-17")
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0]["vehicle_label"], "PT1023")
        self.assertEqual(details[0]["activity"], "Detailed work note")
        self.assertEqual(details[0]["period"], "KW31-2026")
        self.assertEqual(details[0]["description"], "Recorded by: Tester")


if __name__ == "__main__":
    unittest.main()
