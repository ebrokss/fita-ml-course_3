SELECT
    DATE_FORMAT(charge_date, '%Y-%m') AS payment_month,
    COUNT(id) AS payment_count,
    SUM(amount) AS total_amount
FROM
    payments
GROUP BY
    payment_month
ORDER BY
    payment_month;
