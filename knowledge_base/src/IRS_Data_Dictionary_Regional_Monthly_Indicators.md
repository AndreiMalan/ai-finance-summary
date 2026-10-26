# IRS Data Dictionary: Regional Monthly Indicators (REG_LUNAR)

Institutul Roman de Statistica, Directorate for Regional Statistics. Version 4.2, valid for files published from February 2025. This version replaces version 4.1 of March 2024.

## 1 Purpose

### 1.1 Scope of this dictionary

This dictionary describes the structure and content of the REG_LUNAR dataset, which contains monthly economic and social indicators for the 41 counties (judete) of Romania and the Municipality of Bucharest. It defines every column of the dissemination file, the code list of indicators, the units of measure and the conventions for missing and provisional values. It is the authoritative reference for interpreting REG_LUNAR files.

### 1.2 Distribution formats

REG_LUNAR is disseminated as a UTF-8 encoded CSV file with comma separators and as an Excel workbook (.xlsx) with one sheet named REG_LUNAR. Both formats contain the same columns in the same order. The first row contains the column names exactly as listed in Section 2. Files are published on the IRS open data portal on the dates announced in the IRS Release Calendar.

## 2 Column definitions

### 2.1 Overview

The dataset is in long format: each row holds one value of one indicator for one county and one month. The file has exactly five columns.

| Column | Type | Mandatory | Content | Example |
|---|---|---|---|---|
| luna | text, YYYY-MM | yes | Reference month | 2024-03 |
| judet | text | yes | County name, or TOTAL for Romania | Cluj |
| indicator | text, code | yes | Indicator code from Section 3 | RATA_SOMAJ_INREG |
| valoare | decimal number | no | Value of the indicator | 2.4 |
| um | text, code | yes | Unit of measure from Section 4 | % |

### 2.2 luna

The reference month of the observation, written as a four-digit year, a hyphen and a two-digit month (ISO 8601 calendar month), for example 2024-03 for March 2024. For stock indicators, such as the number of registered unemployed persons, the value refers to the last day of the month. For flow indicators, such as earnings, it refers to the whole month.

### 2.3 judet

The territorial unit of the observation. The column contains the county name written without diacritics, exactly as listed in Annex A, or the value TOTAL for the whole of Romania. The Municipality of Bucharest is written Bucuresti. Each county corresponds to one NUTS 3 region; the NUTS 3 code is not included in the file but can be obtained from Annex A. Development regions (NUTS 2) are not included in REG_LUNAR.

### 2.4 indicator

The code of the indicator measured, taken from the code list in Section 3. Codes are written in capital letters without spaces. A code identifies the concept, the statistical source and the seasonal adjustment status. Codes ending in _SA denote seasonally adjusted series; all other series are unadjusted.

### 2.5 valoare

The numeric value of the indicator for the given month and county. In machine-readable files the decimal separator is the point and no thousands separator is used. In printed bulletins IRS uses the decimal comma. A value is left empty when it is confidential, not yet available or not applicable; empty values are never replaced by zero. Values for the latest month are provisional and are revised in the following release, as set out in the IRS Release Calendar and Dissemination Policy.

### 2.6 um

The unit of measure of the value, taken from the code list in Section 4. The unit is part of the definition of the observation. Two values of the same indicator with different units, for example an index on a different base year, are different series and shall not be compared, averaged or merged.

### 2.7 Columns not described in this dictionary

The REG_LUNAR format consists only of the five columns defined in this Section. Files that contain additional columns are not REG_LUNAR files as disseminated by IRS. IRS provides no definition for such columns, and their content shall not be interpreted on the basis of this dictionary.

## 3 Indicator code list

| Code | Indicator | Source | Unit |
|---|---|---|---|
| IPC | Consumer price index, all items | Price survey | indice 2021=100 |
| IPC_VAR_AN | Consumer prices, annual rate of change | Price survey | % |
| IPC_VAR_LUN | Consumer prices, monthly rate of change | Price survey | % |
| SOMERI_INREG | Registered unemployed persons, end of month | Employment agency register | persoane |
| RATA_SOMAJ_INREG | Registered unemployment rate | Employment agency register | % |
| CASTIG_NET | Average monthly net nominal earnings | Enterprise survey | lei |
| SALARIATI | Number of employees, end of month | Enterprise survey | mii persoane |
| IPI | Industrial production index, gross series | Industry survey | indice 2021=100 |
| IPI_SA | Industrial production index, seasonally and calendar adjusted | Industry survey | indice 2021=100 |

### 3.1 Notes on the price indicators

The county consumer price index IPC is compiled from prices collected in the county and weighted with the national CPI weights of the current year, which are derived from the Household Budget Survey. Its index reference period is 2021=100. It is not a harmonised index and differs from the European harmonised series, which use a different weighting basis and the index reference period 2015=100. The differences are explained in the IRS Technical Note on Differences between IRS Regional Series and European Harmonised Series. IPC_VAR_AN is the percentage change between the index of the month and the index of the same month of the previous year, rounded to one decimal.

### 3.2 Notes on the labour market indicators

SOMERI_INREG and RATA_SOMAJ_INREG follow the registered unemployment concept: persons registered as unemployed with the county employment agency. The rate is the number of registered unemployed divided by the civilian labour force of the county (registered unemployed plus civilian employment) and multiplied by 100. These indicators are not unemployment according to the definitions of the International Labour Organization, which are measured by the Labour Force Survey and are not published monthly at county level.

### 3.3 Notes on earnings

CASTIG_NET is the average gross earnings minus income tax, social security and health contributions borne by the employee, for full-time equivalent employees. It is expressed in current lei and is not adjusted for price changes.

## 4 Units of measure

| Code | Meaning |
|---|---|
| % | Percent |
| indice 2021=100 | Index with the average of the year 2021 equal to 100 |
| persoane | Number of persons |
| mii persoane | Thousands of persons |
| lei | Romanian lei (RON), current prices |

## 5 Quality notes

### 5.1 Consistency checks performed by IRS

Before publication IRS verifies that every row has a valid month, county and indicator; that the unit matches the code list; that rates lie between 0 and 100; and that the county values of SOMERI_INREG add up to the TOTAL row.

### 5.2 Known breaks

With the release of the data for January 2022, the price indicators IPC, IPI and IPI_SA were rebased from 2015=100 to 2021=100 and the county price sample was enlarged. Back series for earlier months were rebased by IRS; their rates of change are unchanged except for rounding. In January 2023 the registered unemployment series was affected by a change in registration rules; observations from that month carry a break in series.

## Annex A Counties and NUTS 3 codes

| NUTS 3 code | judet |
|---|---|
| RO111 | Bihor |
| RO112 | Bistrita-Nasaud |
| RO113 | Cluj |
| RO114 | Maramures |
| RO115 | Satu Mare |
| RO116 | Salaj |
| RO121 | Alba |
| RO122 | Brasov |
| RO123 | Covasna |
| RO124 | Harghita |
| RO125 | Mures |
| RO126 | Sibiu |
| RO211 | Bacau |
| RO212 | Botosani |
| RO213 | Iasi |
| RO214 | Neamt |
| RO215 | Suceava |
| RO216 | Vaslui |
| RO221 | Braila |
| RO222 | Buzau |
| RO223 | Constanta |
| RO224 | Galati |
| RO225 | Tulcea |
| RO226 | Vrancea |
| RO311 | Arges |
| RO312 | Calarasi |
| RO313 | Dambovita |
| RO314 | Giurgiu |
| RO315 | Ialomita |
| RO316 | Prahova |
| RO317 | Teleorman |
| RO321 | Bucuresti |
| RO322 | Ilfov |
| RO411 | Dolj |
| RO412 | Gorj |
| RO413 | Mehedinti |
| RO414 | Olt |
| RO415 | Valcea |
| RO421 | Arad |
| RO422 | Caras-Severin |
| RO423 | Hunedoara |
| RO424 | Timis |
