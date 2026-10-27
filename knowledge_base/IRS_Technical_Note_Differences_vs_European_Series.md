# IRS Technical Note: Differences between IRS Regional Series and European Harmonised Series

Institutul Roman de Statistica, Directorate for Methodology and Quality. Technical Note TN-2025-03, version 1.1, 8 May 2025.

## 1 Purpose

### 1.1 Why this note is needed

Users frequently combine IRS national and regional series with the harmonised series compiled for Romania under the standards of the European Statistical Methods Board (ESMB), for example to compare Romanian counties with other European regions. The series often carry similar names, such as "consumer prices" or "unemployment rate", but they are not the same statistics. This note explains the differences, so that apparent conflicts between sources are interpreted correctly and not reported as errors or as economic developments.

### 1.2 Audience

The note is intended for analysts, journalists and developers of data tools who retrieve IRS data from the REG_LUNAR dataset or the IRS online statistical database and harmonised data from European sources.

## 2 Series covered

| IRS series | European harmonised counterpart | Main differences |
|---|---|---|
| IPC, consumer price index, 2021=100 | HICP for Romania, 2015=100 | Base year, weights, coverage |
| IPC_VAR_AN, annual rate of change | HICP annual rate of change | Weights, coverage |
| RATA_SOMAJ_INREG, registered unemployment rate | Unemployment rate, ILO definition | Concept, source, denominator |
| IPI, industrial production index, 2021=100 | Production in industry, 2021=100 | Seasonal adjustment, revisions |
| County data (NUTS 3) | Regional data mostly at NUTS 2 | Territorial level |

## 3 Methodological differences

### 3.1 Coverage and weights of consumer price indices

The IRS consumer price index (CPI, IPC in REG_LUNAR) measures price changes of goods and services purchased by resident households. Its weights are derived from the Household Budget Survey and are updated annually. The harmonised index of consumer prices (HICP) compiled for Romania under ESMB Regulation 2023/14 follows the domestic concept: it covers expenditure on the economic territory by resident and non-resident households, and its weights are derived from household final monetary consumption expenditure in the national accounts. Expenditure of foreign visitors therefore has weight in the HICP but not in the CPI, while some items of resident households abroad are treated differently. As a consequence, the annual rates of the two indices usually differ by up to a few tenths of a percentage point, and occasionally by more in months with large changes in the prices of items whose weights differ between the two indices.

### 3.2 Base reference periods: 2021=100 and 2015=100

IRS price and production indices are published with the index reference period 2021=100, meaning that the average of the twelve monthly indices of 2021 equals 100. This base was introduced with the release of the data for January 2022, when the back series were rebased. The HICP for Romania, like all HICPs under ESMB Regulation 2023/14, is published with the index reference period 2015=100, meaning that the average of 2015 equals 100.

Because the two series are normalised to 100 in different years, their index levels are not comparable. For the same month and the same territory, an index on 2015=100 is higher than an index on 2021=100 by the cumulative price increase between 2015 and 2021. For Romanian consumer prices the 2015=100 level is higher by about 18 % relative to the 2021=100 level (the 2021 average is about 118 on the 2015=100 base), which in 2024 corresponds to more than 20 index points. Such a difference is caused only by the choice of base year. It is not a discrepancy between sources, an error in either source, or evidence of different price developments.

The following rules apply:

- an index on 2021=100 and an index on 2015=100 are different units of measure; their levels shall not be compared, averaged, merged into one series or used to fill each other's gaps;
- rates of change (monthly or annual) computed within each series are not affected by the base year and can be compared, subject to the differences in weights and coverage explained in Section 3.1;
- to compare levels, one series must first be rebased. Rebasing an index from 2015=100 to 2021=100 consists of dividing each value by the average of the twelve monthly values of 2021 on the 2015=100 base and multiplying by 100. Rebasing changes levels but not rates of change, apart from rounding.

Illustrative example. Assume that the average of 2021 of the HICP for Romania on the 2015=100 base is 118.30 and that the HICP for March 2024 is 151.92 (2015=100). On the 2021=100 base the HICP for March 2024 is 151.92 / 118.30 x 100 = 128.42. If the IRS CPI for the same month is 128.47 (2021=100), the remaining gap of 0.05 index points reflects the differences in weights and coverage of Section 3.1, not the base year. Without rebasing, the apparent gap would have been 23.45 index points.

When a dataset contains the same indicator, territory and month from both sources, the unit of measure (um) recorded with each value identifies the base year. Data users and automated tools shall report such pairs as a difference in units and keep both values, each with its source, rather than choosing one or averaging them.

### 3.3 Registered unemployment and unemployment according to ILO definitions

RATA_SOMAJ_INREG is the registered unemployment rate: registered unemployed persons divided by the civilian labour force (registered unemployed plus civilian employment). The European harmonised unemployment rate follows the definitions of the International Labour Organization and is estimated from the Labour Force Survey for persons aged 15 to 74, as set out in the ESMB Methodological Manual on Labour Force and Unemployment Statistics. Registered unemployment excludes job-seekers who do not register and includes registered persons who are not actively seeking work. In Romania the registered rate has generally been lower than the ILO rate at national level, but the gap varies strongly between counties. The two rates shall not be compared as if they measured the same thing, and a change in one is not evidence of a change in the other.

### 3.4 Territorial level

REG_LUNAR provides data for counties (NUTS 3). Harmonised labour market and many other European regional statistics are available only for development regions (NUTS 2) or for the whole country. A county value shall not be compared with the value of the development region to which it belongs. NUTS 2 aggregates can be computed from county data only for additive indicators, such as numbers of persons, and only when data for all counties of the region are available. Rates and indices cannot be aggregated by simple averaging; they require the underlying levels or weights.

### 3.5 Seasonal adjustment

Most REG_LUNAR series are unadjusted. Harmonised monthly series are often published seasonally adjusted. Month-on-month changes of an unadjusted IRS series must not be compared with month-on-month changes of a seasonally adjusted European series. Annual rates of change of unadjusted series are the appropriate basis for comparison.

### 3.6 Rounding and decimal separators

IRS machine-readable files use the decimal point, while IRS press releases use the decimal comma. Annual rates are published with one decimal. Differences of 0.1 percentage points between two published rates can result from rounding alone.

### 3.7 Timing and revisions

The latest month of REG_LUNAR is provisional and is revised in the next release. Harmonised series are revised according to their own policies. Comparisons should use data extracted on the same date and should state the extraction date.

## 4 Reconciliation checklist

| Check | Question | If the answer is no |
|---|---|---|
| Concept | Do both series measure the same concept? | Do not compare; describe each separately |
| Unit and base year | Do both series have the same unit, including the index base? | Rebase or compare rates of change only |
| Territory | Do both refer to the same territorial unit and NUTS level? | Aggregate correctly or do not compare |
| Adjustment | Are both unadjusted or both seasonally adjusted? | Use annual rates of change |
| Vintage | Were both extracted at the same time? | Note the extraction dates |

## 5 Contact

Questions on this note: IRS Directorate for Methodology and Quality, Statistical Information Service.
