-- OpenClaw Trading Agent Database Schema

CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "vector";

-- Trade signals
CREATE TABLE IF NOT EXISTS trade_signals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    symbol VARCHAR(20) NOT NULL,
    direction VARCHAR(4) NOT NULL,
    timeframe VARCHAR(5) NOT NULL,
    entry_price DECIMAL(15,5) NOT NULL,
    sl_price DECIMAL(15,5) NOT NULL,
    tp1_price DECIMAL(15,5) NOT NULL,
    tp2_price DECIMAL(15,5),
    tp3_price DECIMAL(15,5),
    confluence_score DECIMAL(3,1) NOT NULL,
    rr_ratio DECIMAL(5,2) NOT NULL,
    ict_signal JSONB,
    snr_analysis JSONB,
    sr_analysis JSONB,
    manipulation_score DECIMAL(3,1),
    ai_approved BOOLEAN NOT NULL,
    ai_confidence DECIMAL(3,2),
    ai_reasoning TEXT,
    status VARCHAR(20) DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Executed trades
CREATE TABLE IF NOT EXISTS trades (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    signal_id UUID REFERENCES trade_signals(id),
    mt5_ticket BIGINT UNIQUE NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    direction VARCHAR(4) NOT NULL,
    lot_size DECIMAL(10,2) NOT NULL,
    entry_price DECIMAL(15,5) NOT NULL,
    sl_price DECIMAL(15,5) NOT NULL,
    current_sl DECIMAL(15,5),
    tp_price DECIMAL(15,5),
    close_price DECIMAL(15,5),
    pnl DECIMAL(15,2),
    pnl_pips DECIMAL(10,1),
    achieved_rr DECIMAL(5,2),
    partial_closes JSONB DEFAULT '[]',
    status VARCHAR(20) DEFAULT 'open',
    opened_at TIMESTAMP DEFAULT NOW(),
    closed_at TIMESTAMP,
    duration_minutes INTEGER
);

-- Daily performance
CREATE TABLE IF NOT EXISTS daily_performance (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    date DATE UNIQUE NOT NULL,
    total_pnl DECIMAL(15,2) DEFAULT 0,
    total_trades INTEGER DEFAULT 0,
    winning_trades INTEGER DEFAULT 0,
    losing_trades INTEGER DEFAULT 0,
    win_rate DECIMAL(5,2),
    best_trade_pnl DECIMAL(15,2),
    worst_trade_pnl DECIMAL(15,2),
    max_drawdown DECIMAL(5,2),
    avg_rr DECIMAL(5,2),
    balance_start DECIMAL(15,2),
    balance_end DECIMAL(15,2)
);

-- Pattern memory (pgvector)
CREATE TABLE IF NOT EXISTS pattern_memory (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    signal_id UUID REFERENCES trade_signals(id),
    pattern_embedding VECTOR(384),
    pattern_type VARCHAR(50),
    was_profitable BOOLEAN,
    market_conditions JSONB,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pattern_embedding ON pattern_memory
    USING ivfflat (pattern_embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_signals_status ON trade_signals(status);
