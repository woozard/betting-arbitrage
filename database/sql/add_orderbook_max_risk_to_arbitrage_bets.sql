-- 4casters orderbook max taker risk at placement time (nullable; 4casters leg 1 only).
ALTER TABLE arbitrage_bets
    ADD COLUMN orderbook_max_risk DECIMAL(10, 2) NULL AFTER stake;
