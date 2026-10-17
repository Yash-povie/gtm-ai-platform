// Neo4j 5 constraints and indexes for System 5 (Lookalike Account Finder)
CREATE CONSTRAINT account_domain_unique IF NOT EXISTS FOR (a:Account) REQUIRE a.domain IS UNIQUE;
CREATE CONSTRAINT tech_name_unique IF NOT EXISTS FOR (t:Technology) REQUIRE t.name IS UNIQUE;
CREATE CONSTRAINT industry_name_unique IF NOT EXISTS FOR (i:Industry) REQUIRE i.name IS UNIQUE;
CREATE CONSTRAINT investor_name_unique IF NOT EXISTS FOR (inv:Investor) REQUIRE inv.name IS UNIQUE;
CREATE CONSTRAINT contact_id_unique IF NOT EXISTS FOR (c:Contact) REQUIRE c.id IS UNIQUE;
CREATE CONSTRAINT signaltype_name_unique IF NOT EXISTS FOR (s:SignalType) REQUIRE s.name IS UNIQUE;
CREATE INDEX account_customer_idx IF NOT EXISTS FOR (a:Account) ON (a.is_customer);

// Relationships (documented):
// (a:Account)-[:IN_INDUSTRY]->(i:Industry)
// (a:Account)-[:USES_TECH {detected_at, confidence}]->(t:Technology)
// (a:Account)-[:BACKED_BY {round, amount_usd}]->(inv:Investor)
// (c:Contact)-[:WORKS_AT {title, is_current}]->(a:Account)
// (c:Contact)-[:PREVIOUSLY_AT {left_at, was_champion}]->(a_prev:Account)
// (a:Account)-[:EXHIBITED_SIGNAL {occurred_at, score}]->(s:SignalType)
// (a1:Account)-[:LOOKALIKE_OF {similarity_score, shared_tech_count, updated_at}]->(a2:Account)
