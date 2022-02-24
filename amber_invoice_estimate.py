# Copyright (c) 2022 Graham Lea
#
# Redistribution and use in source and binary forms, with or without modification, are permitted provided that the
# following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following
# disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following
# disclaimer in the documentation and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its contributors may be used to endorse or promote
# products derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES,
# INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import argparse
import logging
import sys
import traceback
from typing import List, Dict, Optional, Callable, TypeVar
from zoneinfo import ZoneInfo

import amberelectric
import json5
from amberelectric.api import amber_api, AmberApi
from amberelectric.model.channel import ChannelType
from amberelectric.model.tariff_information import PeriodType
from amberelectric.model.usage import Usage

from sites import get_site
from usage import stream_usage_data
from util import setup_stderr_logging, read_api_token_from_file, check_python_version, RUNTIME_ERROR_STATUS, \
    year_month, last_year_month, INVALID_FILE_FORMAT_STATUS, CANT_CONTINUE_STATUS, YearMonth, TariffCalendar

T = TypeVar("T")


class LineItem:
    def __init__(self, label: str, amount: float, unit_price: float, total_cost_cents: int):
        self.label = label
        self.amount_used = amount
        self.unit_price = unit_price
        self.total_cost = total_cost_cents


Invoice = Dict[str, List[LineItem]]


def create_usage_filter(component_json: dict, filter_property_name: str, filter_type_constructor: Callable[[str], T],
                        usage_attribute_selector: Callable[[Usage], T]) \
        -> Optional[Callable[[Usage], T]]:
    if filter_property_name in component_json:
        filter_texts = component_json[filter_property_name]
        if not isinstance(filter_texts, list):
            raise ValueError(f"'{filter_property_name}' in a tariff component must be a list of values")
        # TODO: Validate the types of the values
        filter_vals = {filter_type_constructor(f) for f in filter_texts}
        return lambda usage: usage_attribute_selector(usage) in filter_vals
    else:
        return None


class TariffComponent:
    dnsp_label: Optional[str]
    amber_label: str
    period_filter: Optional[Callable[[Usage], bool]]
    channel_type_filter: Optional[Callable[[Usage], bool]]
    hour_filter: Optional[Callable[[Usage], bool]]
    working_weekday_filter: Optional[Callable[[Usage], bool]]
    greenpower_filter: Optional[Callable[[bool], bool]]
    feed_in_filter: Optional[Callable[[bool], bool]]
    month_filter: Optional[Callable[[int], bool]]
    per_kwh_price_cents: float
    per_day_price_cents: float
    per_peak_demand_kw_per_day_price_cents: float

    def __init__(self, tariff_component_json: dict, account_timezone: ZoneInfo, calendar: TariffCalendar):
        if "amberLabel" not in tariff_component_json:
            raise ValueError(
                "Required property 'amberLabel' not found in tariff component: " + str(tariff_component_json))
        self.amber_label = tariff_component_json["amberLabel"]
        self.dnsp_label = tariff_component_json.get("dnspLabel")
        self.period_filter = create_usage_filter(tariff_component_json, "periodFilter", PeriodType.from_str,
                                                 lambda usage: usage.tariff_information.period)
        self.channel_type_filter = create_usage_filter(tariff_component_json, "channelTypeFilter", ChannelType.from_str,
                                                       lambda usage: usage.channel_type)
        self.hour_filter = create_usage_filter(tariff_component_json, "hourFilter", lambda x: x,
                                               lambda usage: usage.start_time.astimezone(account_timezone).hour)

        if "workingWeekdayFilter" in tariff_component_json:
            working_weekday_filter_val = tariff_component_json["workingWeekdayFilter"]
            self.working_weekday_filter = \
                lambda usage: calendar.is_working_weekday(usage.date) == working_weekday_filter_val
        else:
            self.working_weekday_filter = None

        if "greenPowerFilter" in tariff_component_json:
            greenpower_pass_val = tariff_component_json["greenPowerFilter"]
            self.greenpower_filter = lambda greenpower_active: greenpower_active == greenpower_pass_val
        else:
            self.greenpower_filter = None

        if "feedInFilter" in tariff_component_json:
            feed_in_pass_val = tariff_component_json["feedInFilter"]
            self.feed_in_filter = lambda feed_in_active: feed_in_active == feed_in_pass_val
        else:
            self.feed_in_filter = None

        if "monthFilter" in tariff_component_json:
            month_values = tariff_component_json["monthFilter"]
            if not (isinstance(month_values, list) and all([isinstance(v, int) for v in month_values])):
                raise ValueError("'monthFilter' in a tariff component must be a list of months as integers")
            self.month_filter = lambda month: month in month_values
        else:
            self.month_filter = None

        self.per_kwh_price_cents = tariff_component_json.get("centsPerKwh")
        self.per_day_price_cents = tariff_component_json.get("centsPerDay")
        self.per_peak_demand_kw_per_day_price_cents = tariff_component_json.get("centsPerPeakDemandKwPerDay")

        if len(list(filter(None, [self.per_kwh_price_cents, self.per_day_price_cents,
                                  self.per_peak_demand_kw_per_day_price_cents]))) != 1:
            raise ValueError(
                "Tariff components should have exactly one of centsPerKwh, centsPerDay, or centsPerPeakDemandKwPerDay")


class Tariff:
    distribution_loss_factor: float
    components: List[TariffComponent]

    def __init__(self, tariff_json: dict, account_timezone: ZoneInfo, calendar: TariffCalendar):
        if not isinstance(tariff_json, dict):
            raise ValueError("Tariff JSON must be an object")
        components = tariff_json.get("components")
        if not isinstance(components, list):
            raise ValueError(
                "Tariff JSON must contain a property 'components', a list of objects (one for each tariff component)")

        self.distribution_loss_factor = tariff_json.get("distributionLossFactor", 1.0)
        self.components = [TariffComponent(cj, account_timezone, calendar) for cj in components]


def main():
    arg_parser = \
        argparse.ArgumentParser(description="Estimates a monthly Amber Electric invoice for a given tariff")

    arg_parser.add_argument(
        "-t", "--api-token", required=False, default=None,
        help="Your Amber Electric API token. Alternatively, you can place your token in a file called 'apitoken'.")

    arg_parser.add_argument(
        "-s", "--site-id", required=False, default=None,
        help="The ID of the site for which to retrieve usage data. Only required if account has more than one site.")

    arg_parser.add_argument("account_config_file", type=argparse.FileType(),
                            help="A JSON5 file describing the account config to use in generating the estimate."
                                 " See files in the accountConfigs/ directory for examples.")

    arg_parser.add_argument("months", type=year_month, nargs="*", default=[last_year_month()],
                            help="A month, or months, to generate invoice estimates for, specified as YYYY-MM."
                                 " Defaults to the last month.")

    args = arg_parser.parse_args()

    setup_stderr_logging()

    api_token = args.api_token.strip() if args.api_token else read_api_token_from_file(arg_parser)
    site_id = args.site_id

    logging.info(f"Loading config...")

    try:
        account_config = json5.load(args.account_config_file)
        assert isinstance(account_config, dict)
        logging.info(f"   Account config loaded from {args.account_config_file.name}")
    except Exception as ex:
        logging.critical("ERROR: The account config file could not be parsed: " + str(ex))
        exit(INVALID_FILE_FORMAT_STATUS)
        raise SystemExit

    timezone_str = account_config.get("timezone")
    try:
        account_timezone = ZoneInfo(timezone_str)
        logging.info(f"   Timezone: {account_timezone.key}")
    except:
        account_timezone = None

    if not account_timezone:
        logging.critical(
            "ERROR: 'timezone' must be in the account config with a valid timezone name, e.g. Australia/Sydney")
        exit(INVALID_FILE_FORMAT_STATUS)
        raise SystemExit

    green_power_active = account_config.get("greenPowerActive")
    if green_power_active is None or not isinstance(green_power_active, bool):
        logging.critical("ERROR: 'greenPowerActive' must be in the account config with a value of true or false")
        exit(INVALID_FILE_FORMAT_STATUS)
        raise SystemExit

    marginal_loss_factor = account_config.get("marginalLossFactor")
    if marginal_loss_factor is None or not isinstance(marginal_loss_factor, float):
        logging.critical("ERROR: 'marginalLossFactor' must be in the account config with a decimal number value")
        exit(INVALID_FILE_FORMAT_STATUS)
        raise SystemExit

    amber_fee_dollars_inc_gst = account_config.get("amberMonthlyFeeInDollarsIncGst")
    if amber_fee_dollars_inc_gst is None or not (
            isinstance(amber_fee_dollars_inc_gst, float) or isinstance(amber_fee_dollars_inc_gst, int)):
        logging.critical("ERROR: 'amberFeeIncGst' must be in the account config with a numeric value")
        exit(INVALID_FILE_FORMAT_STATUS)
        raise SystemExit
    amber_fee_cents_ex_gst = round(amber_fee_dollars_inc_gst * 100 / 1.1)  # Remove GST

    tariff_files_by_channel_type = account_config.get("tariffsByChannelType")
    if tariff_files_by_channel_type is None or not isinstance(tariff_files_by_channel_type, dict):
        logging.critical("ERROR: 'tariffsByChannelType' must be in the account config and be an object mapping"
                         " Channel Type names to tariffs")
        exit(INVALID_FILE_FORMAT_STATUS)
        raise SystemExit

    other_charges_desc = account_config["otherCharges"]
    if other_charges_desc is None or not isinstance(other_charges_desc, str):
        logging.critical("ERROR: 'otherCharges' must be in the account config and be a string")
        exit(INVALID_FILE_FORMAT_STATUS)
        raise SystemExit

    filename = "data/otherCharges/" + other_charges_desc
    try:
        with open(filename) as file_in:
            other_charges_json = json5.load(file_in)
            public_holiday_patterns = other_charges_json.get("publicHolidayDatePatterns")
            if public_holiday_patterns is None or not isinstance(public_holiday_patterns, list):
                logging.critical(
                    "ERROR: 'publicHolidayDatePatterns' must be in the otherCharges config and be a list of strings")
                exit(INVALID_FILE_FORMAT_STATUS)
                raise SystemExit
            calendar = TariffCalendar(public_holiday_patterns)
            other_charges = Tariff(other_charges_json, account_timezone, calendar)
        logging.info(f"   Loaded {filename}")
    except Exception as ex:
        logging.critical(f"ERROR: Failed to load or parse {filename}: " + str(ex))
        exit(CANT_CONTINUE_STATUS)
        raise SystemExit

    tariff_by_channel_type = dict()
    for ct, tariff_desc in tariff_files_by_channel_type.items():
        filename = "data/tariffs/" + tariff_desc
        with open(filename) as file_in:
            general_tariff = Tariff(json5.load(file_in), account_timezone, calendar)
        logging.info(f"   Loaded tariff from {filename}")
        tariff_by_channel_type[ct] = general_tariff

    months = sorted(args.months)

    amber_configuration = amberelectric.Configuration(access_token=api_token)
    client: AmberApi = amber_api.AmberApi.create(amber_configuration)

    site = get_site(client, site_id)

    invoices: Dict[YearMonth, Invoice] = dict()

    for month in months:
        logging.info(f"Calculating invoice for {month}")
        usages: List[Usage] = list(stream_usage_data(client, site.id, month.first_date(), month.last_date()))
        general_usages: List[Usage] = list(filter(lambda u: u.channel_type == ChannelType.GENERAL, usages))
        controlled_usages: List[Usage] = list(filter(lambda u: u.channel_type == ChannelType.CONTROLLED_LOAD, usages))
        feed_in_usages: List[Usage] = list(filter(lambda u: u.channel_type == ChannelType.FEED_IN, usages))
        non_feed_in_usages: List[Usage] = general_usages + controlled_usages
        feed_in_active = len(feed_in_usages) != 0

        invoice = invoices[month] = dict()
        usage_fees = invoice["Usage Fees"] = []

        general_tariff = tariff_by_channel_type[ChannelType.GENERAL.value]
        controlled_tariff = tariff_by_channel_type[ChannelType.CONTROLLED_LOAD.value]

        if general_usages:
            general_wholesale_total_amount_cents = \
                round(sum([u.kwh * u.spot_per_kwh for u in general_usages])
                      * general_tariff.distribution_loss_factor
                      * marginal_loss_factor
                      / 1.1)  # Remove GST!
            general_wholesale_total_kwh = sum([u.kwh for u in general_usages])
            general_per_kwh_average_cost = general_wholesale_total_amount_cents / general_wholesale_total_kwh
            usage_fees.append(
                LineItem("General Usage Wholesale", general_wholesale_total_kwh, general_per_kwh_average_cost,
                         general_wholesale_total_amount_cents)
            )

        if controlled_usages:
            controlled_wholesale_total_amount_cents = \
                round(sum([u.kwh * u.spot_per_kwh for u in controlled_usages])
                      * controlled_tariff.distribution_loss_factor
                      * marginal_loss_factor
                      / 1.1)  # Remove GST!
            controlled_wholesale_total_kwh = sum([u.kwh for u in controlled_usages])
            controlled_per_kwh_average_cost = controlled_wholesale_total_amount_cents / controlled_wholesale_total_kwh
            usage_fees.append(
                LineItem("Controlled Load Wholesale", controlled_wholesale_total_kwh, controlled_per_kwh_average_cost,
                         controlled_wholesale_total_amount_cents)
            )

        if general_usages:
            for component in (c for c in general_tariff.components if c.per_kwh_price_cents):
                if line_item := create_line_for_component(component.amber_label, month, component, green_power_active,
                                                          feed_in_active, account_timezone, general_usages):
                    usage_fees.append(line_item)

        if controlled_usages:
            for component in (c for c in controlled_tariff.components if c.per_kwh_price_cents):
                if line_item := create_line_for_component(component.amber_label, month, component, green_power_active,
                                                          feed_in_active, account_timezone, controlled_usages):
                    usage_fees.append(line_item)

        for component in (c for c in other_charges.components if c.per_kwh_price_cents):
            if line_item := create_line_for_component(component.amber_label, month, component, green_power_active,
                                                      feed_in_active, account_timezone, non_feed_in_usages):
                usage_fees.append(line_item)

        # TODO: I don't actually know how/where Amber puts this on the bill
        demand_components = [c for c in general_tariff.components if c.per_peak_demand_kw_per_day_price_cents]
        if general_usages and demand_components:
            demand_fees = invoice["Peak Demand Fees"] = []
            for component in demand_components:
                if line_item := create_line_for_component(component.amber_label, month, component, green_power_active,
                                                          feed_in_active, account_timezone, general_usages):
                    demand_fees.append(line_item)

        daily_fees = invoice["Daily Supply Fees"] = []
        # TODO: Metering charges not matching the bill. Why?
        #  https://github.com/amberelectric/public-api/discussions/50#discussioncomment-2235337
        if general_usages:
            for component in (c for c in general_tariff.components if c.per_day_price_cents):
                if line_item := create_line_for_component(component.amber_label, month, component, green_power_active,
                                                          feed_in_active, account_timezone, general_usages):
                    daily_fees.append(line_item)

        if controlled_usages:
            for component in (c for c in controlled_tariff.components if c.per_day_price_cents):
                if line_item := create_line_for_component(component.amber_label, month, component, green_power_active,
                                                          feed_in_active, account_timezone, controlled_usages):
                    daily_fees.append(line_item)

        days = month.total_days()
        invoice["Amber Fees"] = [
            LineItem(f"Amber - ${amber_fee_dollars_inc_gst} per month", days, amber_fee_cents_ex_gst / days,
                     amber_fee_cents_ex_gst)
        ]

        if feed_in_usages:
            # NOTE: All Feed-In amounts are calculated as positives, then negated in final line item.
            export_loss_factor = 1 / (general_tariff.distribution_loss_factor * marginal_loss_factor)
            export_wholesale_total_amount_cents = \
                round(sum([u.kwh * u.spot_per_kwh for u in feed_in_usages]) * export_loss_factor)

            # Include feed-in specific market charges in the one export line item
            for component in (c for c in other_charges.components if c.per_kwh_price_cents):
                if line_item := create_line_for_component(component.amber_label, month, component, green_power_active,
                                                          feed_in_active, account_timezone, feed_in_usages):
                    export_wholesale_total_amount_cents += line_item.total_cost

            export_wholesale_total_kwh = sum([u.kwh for u in feed_in_usages])
            export_per_kwh_average_cost = \
                (export_wholesale_total_amount_cents / export_wholesale_total_kwh) if export_wholesale_total_kwh else 0
            invoice["Your Export Credits"] = [
                LineItem("Solar Exports", export_wholesale_total_kwh, export_per_kwh_average_cost,
                         -export_wholesale_total_amount_cents)
            ]

    for month, invoice in invoices.items():
        print("\n" + ("-" * 80))
        print(f"Month: {month}")
        total_cents = 0
        for section, items in invoice.items():
            print(f"\n   {section}:")
            for item in items:
                print(f"      {item.label:35s}   {item.amount_used:6.1f}   {item.unit_price:6.2f}"
                      f"   ${item.total_cost / 100:7.2f}")
                total_cents += item.total_cost

        solar_credits = -invoice["Your Export Credits"][0].total_cost if "Your Export Credits" in invoice else 0
        gst_cents = round((total_cents - solar_credits) * 0.1)  # No GST on exports
        total_cents = total_cents + gst_cents
        print(f"\n   TOTAL (incl. GST): ${total_cents / 100:7.2f}\n")


def create_line_for_component(label: str, month: YearMonth, component: TariffComponent, greenpower_active: bool,
                              feed_in_active: bool, account_timezone, base_usages: List[Usage]) \
        -> Optional[LineItem]:
    component_name = component.dnsp_label or component.amber_label
    # TODO: Should be in the component class!
    if component.greenpower_filter and not component.greenpower_filter(greenpower_active):
        logging.info(
            f"    Ignoring component '{component_name}' due to non-matching greenpower active ({greenpower_active})")
        return None
    if component.feed_in_filter and not component.feed_in_filter(feed_in_active):
        logging.info(f"    Ignoring component '{component_name}' due to non-matching feed-in active ({feed_in_active})")
        return None
    if component.month_filter and not component.month_filter(month.month):
        # TODO: Change to debug
        logging.info(f"    Ignoring component '{component_name}' due to non-matching month ({month.month})")
        return None

    if component.per_day_price_cents:
        amount = month.total_days()
        unit_price = component.per_day_price_cents
    else:
        filtered_usages = filter_usages(base_usages, component)
        if component.per_kwh_price_cents:
            amount = sum([u.kwh for u in filtered_usages])
            unit_price = component.per_kwh_price_cents
        elif component.per_peak_demand_kw_per_day_price_cents:
            # NOTE: This will break if 5 min windows becomes the default?
            filtered_usages = filter_usages(base_usages, component)
            peak_demand_usage = max(filtered_usages, key=lambda u: u.kwh)
            peak_demand_kw = peak_demand_usage.kwh * 2
            logging.info(f"    Peak demand for {month} found at "
                         f"{peak_demand_usage.start_time.astimezone(account_timezone)}"
                         f" = {peak_demand_usage.kwh} kWh in 30 min = {peak_demand_kw} kW")
            amount = month.total_days()
            unit_price = component.per_peak_demand_kw_per_day_price_cents * peak_demand_kw
        else:
            raise RuntimeError("TariffComponent doesn't have any known price property")

    total_cost_cents = round(amount * unit_price)
    return LineItem(label, amount, unit_price, total_cost_cents) if total_cost_cents != 0 else None


def filter_usages(base_usages, component):
    filtered_usages: List[Usage] = base_usages
    if component.period_filter:
        filtered_usages: List[Usage] = list(filter(component.period_filter, filtered_usages))
    if component.channel_type_filter:
        filtered_usages: List[Usage] = list(filter(component.channel_type_filter, filtered_usages))
    if component.hour_filter:
        count_before = len(filtered_usages)
        filtered_usages: List[Usage] = list(filter(component.hour_filter, filtered_usages))
        logging.debug(f"    hour_filter: {count_before} -> {len(filtered_usages)}")
    if component.working_weekday_filter:
        count_before = len(filtered_usages)
        filtered_usages: List[Usage] = list(filter(component.working_weekday_filter, filtered_usages))
        logging.debug(f"    working_weekday_filter: {count_before} -> {len(filtered_usages)}")
    return filtered_usages


if __name__ == '__main__':
    check_python_version()
    try:
        main()
    except Exception as e:
        traceback.print_exc()
        sys.stderr.flush()
        print(f"\nERROR: {e}", file=sys.stderr)
        exit(RUNTIME_ERROR_STATUS)
