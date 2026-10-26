# ESMB Guide to Volume Measures in National Accounts

Version 1.2, November 2022. Guidance issued by the European Statistical Methods Board for compilers and users of quarterly and annual national accounts. The guide is consistent with the System of National Accounts 2008 (2008 SNA) and the European System of Accounts (ESA 2010).

## 1 Scope and purpose

### 1.1 Why volume measures are needed

Aggregates at current prices, such as gross domestic product (GDP) at market prices, change both because quantities and qualities change and because prices change. Economic growth refers only to the first component. Volume measures remove the effect of price change so that the development of production, expenditure and income can be analysed over time.

### 1.2 Terminology

This guide uses the following terms.

(a) "Current prices" means values expressed in the prices of the period to which they refer.

(b) "Previous year prices" means values of year t expressed in the average prices of year t-1.

(c) "Chain-linked volumes" means a time series obtained by linking volume changes measured at previous year prices, expressed in the monetary unit of a chosen reference year.

(d) "Reference year" means the year in which chain-linked volumes are equal to current price values. It is not a base year for the price structure, which changes every year.

(e) "Implicit deflator" means the ratio of a current price value to the corresponding chain-linked volume, multiplied by 100.

The expression "constant prices" refers to a fixed-base method in which all years are valued at the prices of one base year. It is not used under ESA 2010 for official volume series and should not be used to describe chain-linked volumes.

## 2 Chain-linking

### 2.1 Principle

Volume change between year t-1 and year t is measured as a Laspeyres volume index using the prices of year t-1 as weights. The annual indices are multiplied together to obtain a chain index, which is scaled to the current price value of the reference year. Because the price weights are updated every year, chain-linked volumes reflect the current structure of the economy better than fixed-base volumes, whose weights become outdated and bias growth rates (the substitution bias).

### 2.2 Formula

Let V(t) denote the value of an aggregate at current prices and P(t) its value at previous year prices. The volume index of year t relative to year t-1 is L(t) = P(t) / V(t-1). Chain-linked volumes with reference year r are CLV(r) = V(r) and, for later years, CLV(t) = CLV(t-1) x L(t). For years before the reference year the chain is applied backwards by division.

### 2.3 Annual overlap and one-quarter overlap for quarterly data

Quarterly accounts shall be chain-linked with the annual overlap technique: the quarters of year t at average prices of year t-1 are linked to the annual average of year t-1. This preserves the consistency of the quarterly volumes with the annual chain-linked volumes. The one-quarter overlap technique, which links at the fourth quarter, gives smoother quarterly growth but breaks the consistency between quarterly and annual figures and is not recommended for official estimates.

## 3 Non-additivity

### 3.1 The property

Chain-linked volumes are not additive. Except in the reference year and the year following it, the chain-linked volume of an aggregate differs from the sum of the chain-linked volumes of its components. The difference is not an error. It results from the different relative prices used to weight each annual link.

### 3.2 Numerical illustration

An economy produces two products, A and B. In 2022, A grows by 10 % and B falls by 10 % at 2021 prices. In 2023, A grows by 5 % and B falls by 10 % at 2022 prices. The relative price of B rose between 2021 and 2022. Values are in million monetary units; the reference year is 2021.

| Year | A, chain-linked | B, chain-linked | Sum of A and B | Total, chain-linked | Discrepancy |
|---|---|---|---|---|---|
| 2021 | 100.0 | 50.0 | 150.0 | 150.0 | 0.0 |
| 2022 | 110.0 | 45.0 | 155.0 | 155.0 | 0.0 |
| 2023 | 115.5 | 40.5 | 156.0 | 155.6 | -0.4 |

Values at current prices are 121.0 for A and 54.0 for B in 2022. At 2022 prices, A amounts to 127.05 and B to 48.6 in 2023. The total therefore grows by (127.05 + 48.6) / (121.0 + 54.0) = 1.0037, or 0.4 %, while the components summed would suggest 0.6 %.

### 3.3 Consequences for users

(a) Shares of components in a chain-linked total shall not be computed from chain-linked volumes; shares shall be computed at current prices.

(b) Published tables of chain-linked volumes shall state that components do not add up to totals.

(c) Users who need an additive breakdown of growth shall use contributions to growth (Section 4).

## 4 Contributions to growth

### 4.1 Definition

The contribution of a component to the volume growth of an aggregate in year t is the change of that component at previous year prices divided by the aggregate at current prices of year t-1, multiplied by 100. Contributions are expressed in percentage points and add up to the growth rate of the aggregate, subject to rounding.

### 4.2 Example

In the illustration of Section 3.2, the contribution of A to growth in 2023 is (127.05 - 121.0) / 175.0 x 100 = 3.5 percentage points and the contribution of B is (48.6 - 54.0) / 175.0 x 100 = -3.1 percentage points. Together they equal the growth of the total, 0.4 %.

## 5 Prices and deflators

### 5.1 Implicit deflator

The implicit deflator of an aggregate is obtained as current price value / chain-linked volume x 100. It equals 100 in the reference year. It is a Paasche-type price index between adjacent years and reflects changes in the composition of the aggregate, so its movements are not a pure measure of price change.

### 5.2 Relation to consumer price indices

The deflator of household final consumption expenditure and the consumer price index measure different things. The deflator covers imputed rents of owner-occupiers and the whole of household consumption in national accounts, uses current weights, and is revised with the accounts. Consumer price indices use fixed weights within a year and are not revised as a rule. The two growth rates may therefore differ, sometimes by more than one percentage point in a year of strong relative price changes.

## 6 Seasonal and calendar adjustment

### 6.1 Practice

Quarterly volume series shall be published unadjusted, calendar adjusted, and seasonally and calendar adjusted. Quarter-on-quarter growth rates shall be computed from seasonally and calendar adjusted data. Year-on-year growth rates may be computed from calendar adjusted or unadjusted data, and the choice shall be stated.

### 6.2 Direct and indirect adjustment

With chain-linked data, seasonally adjusting the components separately and aggregating them (indirect adjustment) does not reproduce the directly adjusted total because of non-additivity. Compilers shall document which approach is used for GDP and keep it stable over time.

## 7 Revisions

### 7.1 Vintages

Early quarterly estimates are based on incomplete sources and are revised as more complete information arrives. The preliminary flash estimate of GDP growth is followed by a fuller estimate around 60 days after the quarter and by annual and benchmark revisions. Users comparing growth rates shall use a single vintage of the data.

### 7.2 Change of reference year

A change of reference year rescales all chain-linked volumes but does not change their growth rates. It changes the size of the non-additivity discrepancies, which are zero in the new reference year and the year after it.
