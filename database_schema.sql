CREATE DATABASE IF NOT EXISTS legal_rag DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE legal_rag;

CREATE TABLE IF NOT EXISTS users (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    username VARCHAR(80) UNIQUE NOT NULL,
    phone VARCHAR(20) UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    display_name VARCHAR(80),
    role VARCHAR(40) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at DATETIME NOT NULL,
    last_login_at DATETIME NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS sms_verification_codes (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    phone VARCHAR(20) NOT NULL,
    purpose VARCHAR(40) NOT NULL,
    code_hash VARCHAR(128) NOT NULL,
    expires_at DATETIME NOT NULL,
    used_at DATETIME NULL,
    created_at DATETIME NOT NULL,
    INDEX idx_sms_codes_lookup(phone, purpose, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS legal_documents (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    title VARCHAR(255) NOT NULL,
    document_type VARCHAR(40) NOT NULL,
    case_cause VARCHAR(80),
    region VARCHAR(80),
    year INT,
    source_name VARCHAR(255),
    validity_status VARCHAR(40) DEFAULT 'active',
    validity_date VARCHAR(32),
    validity_note TEXT,
    file_hash VARCHAR(128),
    status VARCHAR(40) NOT NULL DEFAULT 'published',
    created_by BIGINT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    FOREIGN KEY(created_by) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS document_chunks (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    document_id BIGINT NOT NULL,
    chunk_no INT NOT NULL,
    content TEXT NOT NULL,
    section_name VARCHAR(120),
    paragraph_no INT,
    vector_id VARCHAR(120),
    metadata_json TEXT,
    FOREIGN KEY(document_id) REFERENCES legal_documents(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS import_jobs (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    file_name VARCHAR(255) NOT NULL,
    file_hash VARCHAR(128),
    stage VARCHAR(80) NOT NULL,
    status VARCHAR(40) NOT NULL,
    total_chunks INT NOT NULL DEFAULT 0,
    error_summary TEXT,
    created_by BIGINT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    FOREIGN KEY(created_by) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS reasoning_tasks (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    user_id BIGINT NOT NULL,
    title VARCHAR(255) NOT NULL,
    case_text MEDIUMTEXT NOT NULL,
    extracted_facts_json TEXT,
    filters_json TEXT,
    status VARCHAR(40) NOT NULL DEFAULT 'draft',
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS reasoning_results (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    task_id BIGINT NOT NULL,
    version_no INT NOT NULL,
    result_markdown MEDIUMTEXT NOT NULL,
    result_json TEXT,
    model_name VARCHAR(120) NOT NULL,
    retrieval_config_json TEXT NOT NULL,
    latency_ms INT,
    created_at DATETIME NOT NULL,
    FOREIGN KEY(task_id) REFERENCES reasoning_tasks(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS citations (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    result_id BIGINT NOT NULL,
    citation_no INT NOT NULL,
    document_id BIGINT NOT NULL,
    chunk_id BIGINT NOT NULL,
    quote_text MEDIUMTEXT NOT NULL,
    relevance_score DOUBLE,
    keyword_score DOUBLE,
    vector_score DOUBLE,
    FOREIGN KEY(result_id) REFERENCES reasoning_results(id),
    FOREIGN KEY(document_id) REFERENCES legal_documents(id),
    FOREIGN KEY(chunk_id) REFERENCES document_chunks(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS feedback (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    user_id BIGINT NOT NULL,
    task_id BIGINT NOT NULL,
    result_id BIGINT NOT NULL,
    citation_id BIGINT,
    rating VARCHAR(40) NOT NULL,
    reason VARCHAR(255),
    comment TEXT,
    created_at DATETIME NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id),
    FOREIGN KEY(task_id) REFERENCES reasoning_tasks(id),
    FOREIGN KEY(result_id) REFERENCES reasoning_results(id),
    FOREIGN KEY(citation_id) REFERENCES citations(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS audit_logs (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    actor_id BIGINT,
    action VARCHAR(80) NOT NULL,
    object_type VARCHAR(80),
    object_id BIGINT,
    result VARCHAR(80) NOT NULL,
    request_id VARCHAR(120),
    created_at DATETIME NOT NULL,
    FOREIGN KEY(actor_id) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS system_configs (
    `key` VARCHAR(80) PRIMARY KEY,
    value TEXT NOT NULL,
    description VARCHAR(255),
    updated_by BIGINT,
    updated_at DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
