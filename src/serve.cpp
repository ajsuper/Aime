#include "sqlite3.h"
#include <iostream>
#include <vector>
#include <sstream>
#include <algorithm>
#include <string>
#include <ctime>
#include <fstream>
#include <filesystem>
#include <cstdlib>
#include <cctype>
#include <mutex>
#include <set>
#include <unordered_map>

#include "../include/crow_all.h"

// Root directory under which every user's database lives. Set from
// $HOME/.local/share/aime-assistant/database (or argv[1]) in main().
// Each user gets:
//     <g_root>/users/<user_id>/database.sql
//     <g_root>/users/<user_id>/topics/<topic_id>_<title>.md
// Auth state and the Flask session secret live one level up at <g_root>/.
static std::string g_root;

// Per-user sqlite handle cache. Crow is multithreaded and sqlite (built in
// the default SERIALIZED mode) is safe to share a connection across threads,
// so we hand out the same handle to whichever worker thread is currently
// servicing that user's request. Never evicted: open handles are cheap and
// the user population for a personal-scale deploy is small.
static std::unordered_map<int, sqlite3*> g_user_dbs;
static std::mutex g_user_dbs_mu;

static std::string userDir(int user_id) {
    return g_root + "/users/" + std::to_string(user_id);
}

static std::string userTopicsDir(int user_id) {
    return userDir(user_id) + "/topics";
}

// ─── Data structures ─────────────────────────────────────────────────────────

struct CalenderEvent {
    int id = -1;
    std::string eventTitle;
    std::string eventSummary;
    std::string eventCategory;
    std::string eventDate;  // DD/MM/YYYY (start date)
    std::string eventTime;  // HH:MM (start time)
    // Event length. Stored as an ABSOLUTE end, mirroring the start's shape:
    // endDate is the (multi-day) end date, endTime the end-of-day wall time.
    // Both default "" = unset, so every legacy event stays a valid point in time.
    // A `duration` input is normalized to these in the Python tool layer before
    // it ever reaches here — the C++ side only ever knows the concrete end.
    std::string eventEndDate;  // DD/MM/YYYY, "" if open-ended / point event
    std::string eventEndTime;  // HH:MM, "" if all-day or point event
    bool eventArchived = false;
    // Commitment-tracking metadata (all additive; older rows default to these).
    std::string commitmentId;                 // stable slug linking recurring instances
    std::string status = "scheduled";         // scheduled / completed / canceled / unknown (unknown is system-set)
    std::string statusChangeReason;           // why the status is what it is (most often a cancel reason)
    std::string rescheduledFrom;              // original DD/MM/YYYY if moved
    std::string createdAt;                    // ISO timestamp, server-stamped on create
    std::string lastModifiedAt;               // ISO timestamp, server-stamped on every write
};

struct Date {
    uint day;
    uint month;
    uint year;
};

struct Time {
    uint hour;
    uint minute;
};

struct FilterOptions {
    // Date range — only applied when filterByDate = true
    bool filterByDate = false;
    Date startDate = {1, 1, 1970};
    Date endDate   = {31, 12, 9999};

    // Time range — only applied when filterByTime = true
    bool filterByTime = false;
    Time startTime = {0, 0};
    Time endTime   = {23, 59};

    // Category: set one or the other, not both
    std::string category;              // exact single-category match
    std::vector<std::string> categories; // OR match across multiple categories

    // Full-text keyword search across title and summary (case-insensitive for ASCII)
    std::string keyword;

    enum class ArchivedFilter { All, ActiveOnly, ArchivedOnly } archived = ArchivedFilter::ActiveOnly;
    enum class SortOrder { DateAsc, DateDesc } sortOrder = SortOrder::DateAsc;

    // Number of filtered items.
    int limit = 0; // 0 = unlimited
};

struct TopicFilterOptions {
    // Category: set one or the other, not both
    std::string category;              // exact single-category match
    std::vector<std::string> categories; // OR match across multiple categories

    // Full-text keyword search across title and summary (case-insensitive for ASCII)
    std::string keyword;
    int limit = 0; // 0 = unlimited
};

struct Topic {
    int id = -1;
    std::string topicTitle;
    std::string topicSummary;
    std::string topicCategory;
    std::string topicFolder; // "" = root (no folder)
};

struct EditPatch {
    std::string find;
    std::string replace;
    int occurrence = 0; // 0 = require a unique match; >=1 = explicit 1-based index
};

struct EditResult {
    bool ok = true;
    std::string error;
    int applied = 0;
};

// ─── Calendar open / schema
void openDatabase(char* filename, sqlite3** database) {
    int opened = sqlite3_open(filename, database);
    if (opened) {
        std::cout << "Failed to open calender: " << sqlite3_errmsg(*database) << std::endl;
    } else {
        std::cout << "Opened calender!" << std::endl;
    }
}

void createCalender(sqlite3* database) {
    char* errMsg = nullptr;
    std::string sqlCommand =
        "CREATE TABLE EVENT("
        "ID INTEGER PRIMARY KEY AUTOINCREMENT,"
        "EVENT_TITLE TEXT NOT NULL,"
        "EVENT_SUMMARY TEXT NOT NULL,"
        "EVENT_CATEGORY TEXT NOT NULL,"
        "EVENT_DATE TEXT NOT NULL,"
        "EVENT_TIME TEXT NOT NULL,"
        "EVENT_ARCHIVED TEXT NOT NULL,"
        // Commitment-tracking columns. Keep this order in sync with rowToEvent's
        // positional reads (indices 7-12) and with the migration ALTERs below.
        "EVENT_COMMITMENT_ID TEXT NOT NULL DEFAULT '',"
        "EVENT_STATUS TEXT NOT NULL DEFAULT 'scheduled',"
        "EVENT_STATUS_CHANGE_REASON TEXT NOT NULL DEFAULT '',"
        "EVENT_RESCHEDULED_FROM TEXT NOT NULL DEFAULT '',"
        "EVENT_CREATED_AT TEXT NOT NULL DEFAULT '',"
        "EVENT_LAST_MODIFIED_AT TEXT NOT NULL DEFAULT '',"
        // Event-length columns (indices 13-14). Must stay last so they line up
        // with the migration ALTERs, which always append.
        "EVENT_END_DATE TEXT NOT NULL DEFAULT '',"
        "EVENT_END_TIME TEXT NOT NULL DEFAULT ''"
        ")";

    int result = sqlite3_exec(database, sqlCommand.c_str(), NULL, 0, &errMsg);
    if (result != SQLITE_OK) {
        std::cout << "Note: createCalender — " << errMsg << std::endl;
    } else {
        std::cout << "Created calender!" << std::endl;
    }

    // Migration: older databases pre-date the commitment-tracking columns. Add
    // any that are missing so existing user data keeps working untouched — each
    // ADD COLUMN backfills existing rows with its DEFAULT, never overwrites them.
    // Order matters: it must match createCalender's DDL and rowToEvent's reads.
    struct ColumnDef { const char* name; const char* ddl; };
    const ColumnDef newColumns[] = {
        {"EVENT_COMMITMENT_ID",    "EVENT_COMMITMENT_ID TEXT NOT NULL DEFAULT ''"},
        {"EVENT_STATUS",                "EVENT_STATUS TEXT NOT NULL DEFAULT 'scheduled'"},
        {"EVENT_STATUS_CHANGE_REASON",  "EVENT_STATUS_CHANGE_REASON TEXT NOT NULL DEFAULT ''"},
        {"EVENT_RESCHEDULED_FROM",      "EVENT_RESCHEDULED_FROM TEXT NOT NULL DEFAULT ''"},
        {"EVENT_CREATED_AT",       "EVENT_CREATED_AT TEXT NOT NULL DEFAULT ''"},
        {"EVENT_LAST_MODIFIED_AT", "EVENT_LAST_MODIFIED_AT TEXT NOT NULL DEFAULT ''"},
        {"EVENT_END_DATE",         "EVENT_END_DATE TEXT NOT NULL DEFAULT ''"},
        {"EVENT_END_TIME",         "EVENT_END_TIME TEXT NOT NULL DEFAULT ''"},
    };

    std::set<std::string> existingColumns;
    sqlite3_stmt* infoStmt;
    if (sqlite3_prepare_v2(database, "PRAGMA table_info(EVENT)", -1, &infoStmt, nullptr) == SQLITE_OK) {
        while (sqlite3_step(infoStmt) == SQLITE_ROW) {
            const unsigned char* col = sqlite3_column_text(infoStmt, 1);
            if (col) existingColumns.insert(reinterpret_cast<const char*>(col));
        }
        sqlite3_finalize(infoStmt);
    }

    // Rename migration: EVENT_CANCEL_REASON became EVENT_STATUS_CHANGE_REASON.
    // It's the same data — a cancel reason is just one kind of status-change
    // reason — so rename the column in place to carry existing values over,
    // rather than letting the add-column loop below create a fresh empty one
    // (which would orphan the old data). Brand-new DBs already have the new
    // name from createCalender's DDL and skip this. RENAME COLUMN preserves the
    // column's ordinal position, so rowToEvent's positional reads stay valid.
    if (existingColumns.count("EVENT_CANCEL_REASON") &&
        !existingColumns.count("EVENT_STATUS_CHANGE_REASON")) {
        const char* rename =
            "ALTER TABLE EVENT RENAME COLUMN EVENT_CANCEL_REASON TO EVENT_STATUS_CHANGE_REASON";
        char* renameErr = nullptr;
        if (sqlite3_exec(database, rename, NULL, 0, &renameErr) == SQLITE_OK) {
            std::cout << "Renamed EVENT_CANCEL_REASON to EVENT_STATUS_CHANGE_REASON." << std::endl;
            existingColumns.erase("EVENT_CANCEL_REASON");
            existingColumns.insert("EVENT_STATUS_CHANGE_REASON");
        } else if (renameErr) {
            std::cout << "Note: EVENT_STATUS_CHANGE_REASON rename — " << renameErr << std::endl;
            sqlite3_free(renameErr);
        }
    }

    for (const auto& column : newColumns) {
        if (existingColumns.count(column.name)) continue;
        std::string alter = std::string("ALTER TABLE EVENT ADD COLUMN ") + column.ddl;
        char* alterErr = nullptr;
        if (sqlite3_exec(database, alter.c_str(), NULL, 0, &alterErr) == SQLITE_OK) {
            std::cout << "Added " << column.name << " column to existing EVENT table." << std::endl;
        } else if (alterErr) {
            std::cout << "Note: " << column.name << " migration — " << alterErr << std::endl;
            sqlite3_free(alterErr);
        }
    }

    // Status migration: 'rescheduled' was retired as a status. A moved event is
    // just 'scheduled' again at its new date (the move is recorded in
    // EVENT_RESCHEDULED_FROM), so fold any legacy 'rescheduled' rows back to
    // 'scheduled'. Idempotent — once converted there's nothing left to match.
    {
        const char* fold =
            "UPDATE EVENT SET EVENT_STATUS='scheduled' WHERE EVENT_STATUS='rescheduled'";
        char* foldErr = nullptr;
        if (sqlite3_exec(database, fold, NULL, 0, &foldErr) == SQLITE_OK) {
            const int changed = sqlite3_changes(database);
            if (changed > 0)
                std::cout << "Migrated " << changed
                          << " 'rescheduled' event(s) back to 'scheduled'." << std::endl;
        } else if (foldErr) {
            std::cout << "Note: rescheduled→scheduled migration — " << foldErr << std::endl;
            sqlite3_free(foldErr);
        }
    }
}

void createTopics(sqlite3* database) {
    char* errMsg = nullptr;
    std::string sqlCommand =
        "CREATE TABLE TOPIC("
        "ID INTEGER PRIMARY KEY AUTOINCREMENT,"
        "TOPIC_TITLE TEXT NOT NULL,"
        "TOPIC_SUMMARY TEXT NOT NULL,"
        "TOPIC_CATEGORY TEXT NOT NULL,"
        "TOPIC_FOLDER TEXT NOT NULL DEFAULT ''"
        ")"; // Topics will be storn as .md file in database/topics/, topic title will be id__topic_name_no_special_char.md

    int result = sqlite3_exec(database, sqlCommand.c_str(), NULL, 0, &errMsg);
    if (result != SQLITE_OK) {
        std::cout << "Note: createTopics — " << errMsg << std::endl;
    } else {
        std::cout << "Created topics!" << std::endl;
    }

    // Migration: older databases pre-date TOPIC_FOLDER. Add it if missing so
    // existing user data keeps working without a manual migration step.
    sqlite3_stmt* stmt;
    bool hasFolder = false;
    if (sqlite3_prepare_v2(database, "PRAGMA table_info(TOPIC)", -1, &stmt, nullptr) == SQLITE_OK) {
        while (sqlite3_step(stmt) == SQLITE_ROW) {
            const unsigned char* col = sqlite3_column_text(stmt, 1);
            if (col && std::string(reinterpret_cast<const char*>(col)) == "TOPIC_FOLDER") {
                hasFolder = true;
                break;
            }
        }
        sqlite3_finalize(stmt);
    }
    if (!hasFolder) {
        char* alterErr = nullptr;
        if (sqlite3_exec(database,
                "ALTER TABLE TOPIC ADD COLUMN TOPIC_FOLDER TEXT NOT NULL DEFAULT ''",
                NULL, 0, &alterErr) == SQLITE_OK) {
            std::cout << "Added TOPIC_FOLDER column to existing TOPIC table." << std::endl;
        } else if (alterErr) {
            std::cout << "Note: TOPIC_FOLDER migration — " << alterErr << std::endl;
            sqlite3_free(alterErr);
        }
    }
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

static std::string sanitizeFileName(const std::string& input) {
    std::string result;
    for (char c : input) {
        if (std::isalnum(c) || c == '_') {
            result += std::tolower(c);
        } else if (c == ' ') {
            result += '_';
        }
    }
    return result;
}

// Reads a TEXT column, returning "" for SQL NULL instead of dereferencing a
// null pointer (older rows may predate a column before its migration runs).
static std::string columnTextOrEmpty(sqlite3_stmt* stmt, int index) {
    const unsigned char* text = sqlite3_column_text(stmt, index);
    return text ? reinterpret_cast<const char*>(text) : "";
}

static CalenderEvent rowToEvent(sqlite3_stmt* stmt) {
    CalenderEvent event;
    event.id           = sqlite3_column_int(stmt, 0);
    event.eventTitle   = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 1));
    event.eventSummary = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 2));
    event.eventCategory = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 3));
    event.eventDate    = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 4));
    event.eventTime    = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 5));
    std::string archived = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 6));
    event.eventArchived = (archived == "TRUE");
    // Commitment-tracking columns (indices match createCalender's DDL order).
    event.commitmentId    = columnTextOrEmpty(stmt, 7);
    std::string status    = columnTextOrEmpty(stmt, 8);
    if (!status.empty()) event.status = status;  // keep the "scheduled" default if blank
    event.statusChangeReason = columnTextOrEmpty(stmt, 9);
    event.rescheduledFrom = columnTextOrEmpty(stmt, 10);
    event.createdAt       = columnTextOrEmpty(stmt, 11);
    event.lastModifiedAt  = columnTextOrEmpty(stmt, 12);
    event.eventEndDate    = columnTextOrEmpty(stmt, 13);
    event.eventEndTime    = columnTextOrEmpty(stmt, 14);
    return event;
}

static Date parseDate(const std::string& dateStr) {
    Date date = {0, 0, 0};
    char sep;
    std::istringstream ss(dateStr);
    ss >> date.day >> sep >> date.month >> sep >> date.year;
    return date;
}

static Time parseTime(const std::string& timeStr) {
    Time t = {0, 0};
    char sep;
    std::istringstream ss(timeStr);
    ss >> t.hour >> sep >> t.minute;
    return t;
}

static int packDate(const Date& d) { return d.year * 10000 + d.month * 100 + d.day; }
static int packTime(const Time& t) { return t.hour * 100 + t.minute; }

static bool dateInRange(const Date& d, const Date& start, const Date& end) {
    return packDate(d) >= packDate(start) && packDate(d) <= packDate(end);
}

static bool timeInRange(const Time& t, const Time& start, const Time& end) {
    return packTime(t) >= packTime(start) && packTime(t) <= packTime(end);
}

// ─── CRUD ─────────────────────────────────────────────────────────────────────

// Current UTC time as an ISO-8601 string (e.g. "2026-06-01T14:30:00Z"). Used to
// stamp created_at / last_modified_at server-side so clients can't skew them.
static std::string isoTimestampNow() {
    std::time_t now = std::time(nullptr);
    std::tm utc{};
#if defined(_WIN32)
    gmtime_s(&utc, &now);
#else
    gmtime_r(&now, &utc);
#endif
    char buf[32];
    std::strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%SZ", &utc);
    return buf;
}

// The only lifecycle states an event may hold. `unknown` is included because
// it's a legitimate stored value (the system sets it) and clients may echo it
// back on an unrelated edit — but it's not something a client should set fresh;
// that's enforced by guidance, not here. Anything outside this set is a bug
// (a typo or a synonym like "done") and is rejected so it can't silently split
// the data the pattern tools aggregate on.
static bool isValidStatus(const std::string& status) {
    return status == "scheduled" || status == "completed" ||
           status == "canceled"  || status == "unknown";
}

// Sweep still-`scheduled` events whose moment has passed over to `unknown`.
//
// A `scheduled` event the user never resolved doesn't mean it didn't happen —
// it means we don't *know*. Leaving it `scheduled` forever is misleading (it
// reads as still-upcoming) and quietly corrupts streaks, so once its time is
// past we flip it to `unknown`, which is an honest "needs a human to say."
// Only `scheduled` is touched — completed / canceled are real outcomes and
// stay put; an already-`unknown` event past won't re-match either.
//
// `nowDate`/`nowTime` come from the caller in the *user's* local time (the C++
// side only knows UTC), so the boundary matches what the user sees as "now".
//
// "Past" is judged by the event's END, never its start, so an in-progress event
// is never prematurely marked unconfirmed:
//   * end_date + end_time  → past once now is beyond that exact instant;
//   * end_date, no end_time → past once the whole end day has elapsed;
//   * no end (a single-day all-day item OR a point event with just a start
//     time) → past only once its own day is fully behind us — never mid-day.
//
// This is also self-healing: an event previously swept to `unknown` whose end
// turns out NOT to be past (e.g. it was flipped under the old start-based rule,
// or its end was pushed later) is flipped back to `scheduled`. Only these two
// system-derived transitions happen here — completed / canceled are real
// outcomes and are never touched. last_modified is intentionally left alone:
// this is a system reconciliation, not a user edit, and bumping it would spam
// the frontend's "edited since you last looked" tagging.
static void reconcileStalePastEvents(sqlite3* database,
                                     const std::string& nowDate,
                                     const std::string& nowTime) {
    if (nowDate.empty()) return;  // no trustworthy "now" → don't guess
    const int nowDatePacked = packDate(parseDate(nowDate));
    const int nowTimePacked = packTime(parseTime(nowTime));

    std::vector<int> toUnknown;    // scheduled, now past their end
    std::vector<int> toScheduled;  // unknown, but not actually past → heal
    sqlite3_stmt* sel = nullptr;
    const std::string selSql =
        "SELECT ID, EVENT_DATE, EVENT_END_DATE, EVENT_END_TIME, EVENT_STATUS FROM EVENT "
        "WHERE EVENT_STATUS IN ('scheduled','unknown') AND EVENT_ARCHIVED='FALSE'";
    if (sqlite3_prepare_v2(database, selSql.c_str(), -1, &sel, nullptr) != SQLITE_OK) {
        std::cout << "reconcileStalePastEvents select failed: "
                  << sqlite3_errmsg(database) << std::endl;
        return;
    }
    while (sqlite3_step(sel) == SQLITE_ROW) {
        const int id = sqlite3_column_int(sel, 0);
        const std::string dateStr = columnTextOrEmpty(sel, 1);
        const std::string endDateStr = columnTextOrEmpty(sel, 2);
        const std::string endTimeStr = columnTextOrEmpty(sel, 3);
        const std::string status = columnTextOrEmpty(sel, 4);
        const int datePacked = packDate(parseDate(dateStr));

        bool isPast;
        if (!endDateStr.empty()) {
            const int endDatePacked = packDate(parseDate(endDateStr));
            if (!endTimeStr.empty()) {
                // Precise end: past once now has moved beyond end date+time.
                isPast = endDatePacked < nowDatePacked ||
                         (endDatePacked == nowDatePacked &&
                          packTime(parseTime(endTimeStr)) < nowTimePacked);
            } else {
                // End date but no end time: past once the end day is behind us.
                isPast = endDatePacked < nowDatePacked;
            }
        } else {
            // No recorded end (single-day all-day or point event): past only
            // once the whole day has elapsed, regardless of any start time.
            isPast = datePacked < nowDatePacked;
        }

        if (status == "scheduled" && isPast) toUnknown.push_back(id);
        else if (status == "unknown" && !isPast) toScheduled.push_back(id);
    }
    sqlite3_finalize(sel);
    if (toUnknown.empty() && toScheduled.empty()) return;

    auto applyStatus = [&](const std::vector<int>& ids, const char* newStatus) {
        if (ids.empty()) return;
        sqlite3_stmt* upd = nullptr;
        const std::string updSql =
            std::string("UPDATE EVENT SET EVENT_STATUS=? WHERE ID=?");
        if (sqlite3_prepare_v2(database, updSql.c_str(), -1, &upd, nullptr) != SQLITE_OK) {
            std::cout << "reconcileStalePastEvents update failed: "
                      << sqlite3_errmsg(database) << std::endl;
            return;
        }
        for (const int id : ids) {
            sqlite3_bind_text(upd, 1, newStatus, -1, SQLITE_STATIC);
            sqlite3_bind_int(upd, 2, id);
            sqlite3_step(upd);
            sqlite3_reset(upd);
        }
        sqlite3_finalize(upd);
    };
    applyStatus(toUnknown, "unknown");
    applyStatus(toScheduled, "scheduled");
}

void addEvent(sqlite3* database, CalenderEvent& event) {
    sqlite3_stmt* stmt;
    const std::string sql =
        "INSERT INTO EVENT(EVENT_TITLE, EVENT_SUMMARY, EVENT_CATEGORY, EVENT_DATE, EVENT_TIME, EVENT_ARCHIVED,"
        " EVENT_COMMITMENT_ID, EVENT_STATUS, EVENT_STATUS_CHANGE_REASON, EVENT_RESCHEDULED_FROM,"
        " EVENT_CREATED_AT, EVENT_LAST_MODIFIED_AT, EVENT_END_DATE, EVENT_END_TIME)"
        " VALUES(?, ?, ?, ?, ?, 'FALSE', ?, ?, ?, ?, ?, ?, ?, ?)";

    if (sqlite3_prepare_v2(database, sql.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        std::cout << "addEvent prepare failed: " << sqlite3_errmsg(database) << std::endl;
        return;
    }

    const std::string now = isoTimestampNow();
    event.createdAt = now;
    event.lastModifiedAt = now;
    const std::string status = event.status.empty() ? "scheduled" : event.status;

    sqlite3_bind_text(stmt, 1, event.eventTitle.c_str(),     -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 2, event.eventSummary.c_str(),   -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 3, event.eventCategory.c_str(),  -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 4, event.eventDate.c_str(),      -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 5, event.eventTime.c_str(),      -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 6, event.commitmentId.c_str(),   -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 7, status.c_str(),               -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(stmt, 8, event.statusChangeReason.c_str(), -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 9, event.rescheduledFrom.c_str(),-1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 10, event.createdAt.c_str(),     -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 11, event.lastModifiedAt.c_str(),-1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 12, event.eventEndDate.c_str(),  -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 13, event.eventEndTime.c_str(),  -1, SQLITE_STATIC);

    if (sqlite3_step(stmt) != SQLITE_DONE) {
        std::cout << "addEvent failed: " << sqlite3_errmsg(database) << std::endl;
    }

    sqlite3_finalize(stmt);
    event.id = static_cast<int>(sqlite3_last_insert_rowid(database));
}

CalenderEvent getEvent(sqlite3* database, int eventID) {
    CalenderEvent event;
    sqlite3_stmt* stmt;
    const std::string sql = "SELECT * FROM EVENT WHERE ID=?";

    if (sqlite3_prepare_v2(database, sql.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        std::cout << "getEvent prepare failed: " << sqlite3_errmsg(database) << std::endl;
        return event;
    }

    sqlite3_bind_int(stmt, 1, eventID);

    if (sqlite3_step(stmt) == SQLITE_ROW) {
        event = rowToEvent(stmt);
    } else {
        std::cout << "Event " << eventID << " not found." << std::endl;
    }

    sqlite3_finalize(stmt);
    return event;
}

// Updates all fields except ID and archived status. created_at is preserved;
// last_modified_at is re-stamped server-side on every write.
void updateEvent(sqlite3* database, const CalenderEvent& event) {
    sqlite3_stmt* stmt;
    const std::string sql =
        "UPDATE EVENT SET EVENT_TITLE=?, EVENT_SUMMARY=?, EVENT_CATEGORY=?, EVENT_DATE=?, EVENT_TIME=?,"
        " EVENT_COMMITMENT_ID=?, EVENT_STATUS=?, EVENT_STATUS_CHANGE_REASON=?, EVENT_RESCHEDULED_FROM=?,"
        " EVENT_LAST_MODIFIED_AT=?, EVENT_END_DATE=?, EVENT_END_TIME=?"
        " WHERE ID=?";

    if (sqlite3_prepare_v2(database, sql.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        std::cout << "updateEvent prepare failed: " << sqlite3_errmsg(database) << std::endl;
        return;
    }

    const std::string now = isoTimestampNow();
    const std::string status = event.status.empty() ? "scheduled" : event.status;

    sqlite3_bind_text(stmt, 1, event.eventTitle.c_str(),     -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 2, event.eventSummary.c_str(),   -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 3, event.eventCategory.c_str(),  -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 4, event.eventDate.c_str(),      -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 5, event.eventTime.c_str(),      -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 6, event.commitmentId.c_str(),   -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 7, status.c_str(),               -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(stmt, 8, event.statusChangeReason.c_str(), -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 9, event.rescheduledFrom.c_str(),-1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 10, now.c_str(),                 -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(stmt, 11, event.eventEndDate.c_str(),  -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 12, event.eventEndTime.c_str(),  -1, SQLITE_STATIC);
    sqlite3_bind_int (stmt, 13, event.id);

    if (sqlite3_step(stmt) != SQLITE_DONE) {
        std::cout << "updateEvent failed: " << sqlite3_errmsg(database) << std::endl;
    }

    sqlite3_finalize(stmt);
}

void archiveEvent(sqlite3* database, int eventID) {
    sqlite3_stmt* stmt;
    const std::string sql = "UPDATE EVENT SET EVENT_ARCHIVED='TRUE' WHERE ID=?";

    if (sqlite3_prepare_v2(database, sql.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        std::cout << "archiveEvent prepare failed: " << sqlite3_errmsg(database) << std::endl;
        return;
    }

    sqlite3_bind_int(stmt, 1, eventID);

    if (sqlite3_step(stmt) != SQLITE_DONE) {
        std::cout << "archiveEvent failed: " << sqlite3_errmsg(database) << std::endl;
    }

    sqlite3_finalize(stmt);
}

void unarchiveEvent(sqlite3* database, int eventID) {
    sqlite3_stmt* stmt;
    const std::string sql = "UPDATE EVENT SET EVENT_ARCHIVED='FALSE' WHERE ID=?";

    if (sqlite3_prepare_v2(database, sql.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        std::cout << "unarchiveEvent prepare failed: " << sqlite3_errmsg(database) << std::endl;
        return;
    }

    sqlite3_bind_int(stmt, 1, eventID);

    if (sqlite3_step(stmt) != SQLITE_DONE) {
        std::cout << "unarchiveEvent failed: " << sqlite3_errmsg(database) << std::endl;
    }

    sqlite3_finalize(stmt);
}

// ─── Core filter engine ───────────────────────────────────────────────────────

// All filtering goes through here. SQL handles archived/category/keyword; C++ handles
// date and time ranges (since DD/MM/YYYY strings don't sort lexicographically).
std::vector<CalenderEvent> filterEvents(sqlite3* database, const FilterOptions& opts) {
    std::string sql = "SELECT * FROM EVENT";
    std::vector<std::string> conditions;
    std::vector<std::string> params; // positional bind values

    // Archived
    if (opts.archived == FilterOptions::ArchivedFilter::ActiveOnly) {
        conditions.push_back("EVENT_ARCHIVED = 'FALSE'");
    } else if (opts.archived == FilterOptions::ArchivedFilter::ArchivedOnly) {
        conditions.push_back("EVENT_ARCHIVED = 'TRUE'");
    }

    // Single category — COLLATE NOCASE so category names match regardless of
    // case (ASCII), the same case-insensitivity the keyword LIKE below gets.
    if (!opts.category.empty()) {
        conditions.push_back("EVENT_CATEGORY = ? COLLATE NOCASE");
        params.push_back(opts.category);
    } else if (!opts.categories.empty()) {
        // Multi-category OR
        std::string inClause = "EVENT_CATEGORY IN (";
        for (size_t i = 0; i < opts.categories.size(); i++) {
            inClause += (i == 0 ? "?" : ",?");
            params.push_back(opts.categories[i]);
        }
        inClause += ") COLLATE NOCASE";
        conditions.push_back(inClause);
    }

    // Keyword — case-insensitive for ASCII via SQLite LIKE
    if (!opts.keyword.empty()) {
        conditions.push_back("(EVENT_TITLE LIKE ? OR EVENT_SUMMARY LIKE ?)");
        std::string like = "%" + opts.keyword + "%";
        params.push_back(like);
        params.push_back(like);
    }

    if (!conditions.empty()) {
        sql += " WHERE ";
        for (size_t i = 0; i < conditions.size(); i++) {
            if (i > 0) sql += " AND ";
            sql += conditions[i];
        }
    }

    sqlite3_stmt* stmt;
    if (sqlite3_prepare_v2(database, sql.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        std::cout << "filterEvents prepare failed: " << sqlite3_errmsg(database) << std::endl;
        return {};
    }

    for (size_t i = 0; i < params.size(); i++) {
        sqlite3_bind_text(stmt, static_cast<int>(i + 1), params[i].c_str(), -1, SQLITE_TRANSIENT);
    }

    std::vector<CalenderEvent> results;
    while (sqlite3_step(stmt) == SQLITE_ROW) {
        results.push_back(rowToEvent(stmt));
    }
    sqlite3_finalize(stmt);

    // Date range (C++ post-filter)
    if (opts.filterByDate) {
        results.erase(
            std::remove_if(results.begin(), results.end(), [&](const CalenderEvent& e) {
                return !dateInRange(parseDate(e.eventDate), opts.startDate, opts.endDate);
            }),
            results.end()
        );
    }

    // Time range (C++ post-filter)
    if (opts.filterByTime) {
        results.erase(
            std::remove_if(results.begin(), results.end(), [&](const CalenderEvent& e) {
                return !timeInRange(parseTime(e.eventTime), opts.startTime, opts.endTime);
            }),
            results.end()
        );
    }

    // Sort by date (then by time as a tiebreaker, always ascending)
    std::sort(results.begin(), results.end(), [&](const CalenderEvent& a, const CalenderEvent& b) {
        int da = packDate(parseDate(a.eventDate));
        int db = packDate(parseDate(b.eventDate));
        if (da != db) {
            return opts.sortOrder == FilterOptions::SortOrder::DateAsc ? da < db : da > db;
        }
        return packTime(parseTime(a.eventTime)) < packTime(parseTime(b.eventTime));
    });

    if (opts.limit > 0 && static_cast<int>(results.size()) > opts.limit) {
        results.resize(opts.limit);
    }

    return results;
}

// ─── Convenience filter functions ────────────────────────────────────────────

std::vector<CalenderEvent> getEventsByDateRange(sqlite3* database, Date startDate, Date endDate) {
    FilterOptions opts;
    opts.filterByDate = true;
    opts.startDate    = startDate;
    opts.endDate      = endDate;
    opts.archived     = FilterOptions::ArchivedFilter::All;
    return filterEvents(database, opts);
}

std::vector<CalenderEvent> getEventsByTimeRange(sqlite3* database, Time startTime, Time endTime) {
    FilterOptions opts;
    opts.filterByTime = true;
    opts.startTime    = startTime;
    opts.endTime      = endTime;
    opts.archived     = FilterOptions::ArchivedFilter::All;
    return filterEvents(database, opts);
}

std::vector<CalenderEvent> getEventsByCategory(sqlite3* database, const std::string& category) {
    FilterOptions opts;
    opts.category = category;
    opts.archived = FilterOptions::ArchivedFilter::All;
    return filterEvents(database, opts);
}

// Case-insensitive keyword search across title and summary.
std::vector<CalenderEvent> searchEvents(sqlite3* database, const std::string& keyword) {
    FilterOptions opts;
    opts.keyword  = keyword;
    opts.archived = FilterOptions::ArchivedFilter::All;
    return filterEvents(database, opts);
}

std::vector<CalenderEvent> getActiveEvents(sqlite3* database) {
    FilterOptions opts;
    opts.archived = FilterOptions::ArchivedFilter::ActiveOnly;
    return filterEvents(database, opts);
}

std::vector<CalenderEvent> getArchivedEvents(sqlite3* database) {
    FilterOptions opts;
    opts.archived = FilterOptions::ArchivedFilter::ArchivedOnly;
    return filterEvents(database, opts);
}

// ─── Topic filter engine ─────────────────────────────────────────────────────
Topic rowToTopic(sqlite3_stmt* stmt) {
    Topic topic;
    topic.id = sqlite3_column_int(stmt, 0);
    topic.topicTitle = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 1));
    topic.topicSummary = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 2));
    topic.topicCategory = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 3));
    const unsigned char* folder = sqlite3_column_text(stmt, 4);
    topic.topicFolder = folder ? reinterpret_cast<const char*>(folder) : "";
    return topic;
}


void addTopic(sqlite3* database, int user_id, Topic& topic) {
    sqlite3_stmt* stmt;
    const std::string sql =
        "INSERT INTO TOPIC(TOPIC_TITLE, TOPIC_SUMMARY, TOPIC_CATEGORY, TOPIC_FOLDER)"
        " VALUES(?, ?, ?, ?)";

    if (sqlite3_prepare_v2(database, sql.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        std::cout << "addTopic prepare failed: " << sqlite3_errmsg(database) << std::endl;
        return;
    }

    sqlite3_bind_text(stmt, 1, topic.topicTitle.c_str(),    -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 2, topic.topicSummary.c_str(),  -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 3, topic.topicCategory.c_str(), -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 4, topic.topicFolder.c_str(),   -1, SQLITE_STATIC);

    if (sqlite3_step(stmt) != SQLITE_DONE) {
        std::cout << "addTopic failed: " << sqlite3_errmsg(database) << std::endl;
    }

    sqlite3_finalize(stmt);
    topic.id = static_cast<int>(sqlite3_last_insert_rowid(database));

    std::filesystem::create_directories(userTopicsDir(user_id));
    std::string fileName = userTopicsDir(user_id) + "/" + std::to_string(topic.id) + "_" + sanitizeFileName(topic.topicTitle) + ".md";
    std::ofstream topicFile(fileName);
    if (!topicFile.is_open()) {
        std::cout << "Failed to create topic file: " << fileName << std::endl;
        return;
    }

    topicFile << "# " << topic.topicTitle << "\n\n"
              << "**Category:** " << topic.topicCategory << "\n\n"
              << "**Summary:** " << topic.topicSummary << "\n\n"
              << "---\n\n"
              << "## Content\n\n";
    topicFile.close();
}

// Updates topic summary and category. Title is intentionally not updated, since
// the on-disk filename is derived from the title and renaming the .md file is
// out of scope for this endpoint.
void updateTopic(sqlite3* database, const Topic& topic) {
    sqlite3_stmt* stmt;
    const std::string sql =
        "UPDATE TOPIC SET TOPIC_SUMMARY=?, TOPIC_CATEGORY=?, TOPIC_FOLDER=? WHERE ID=?";

    if (sqlite3_prepare_v2(database, sql.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        std::cout << "updateTopic prepare failed: " << sqlite3_errmsg(database) << std::endl;
        return;
    }

    sqlite3_bind_text(stmt, 1, topic.topicSummary.c_str(),  -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 2, topic.topicCategory.c_str(), -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 3, topic.topicFolder.c_str(),   -1, SQLITE_STATIC);
    sqlite3_bind_int (stmt, 4, topic.id);

    if (sqlite3_step(stmt) != SQLITE_DONE) {
        std::cout << "updateTopic failed: " << sqlite3_errmsg(database) << std::endl;
    }

    sqlite3_finalize(stmt);
}

Topic getTopic(sqlite3* database, int topicID) {
    Topic topic;
    sqlite3_stmt* stmt;
    const std::string sql = "SELECT * FROM TOPIC WHERE ID=?";

    if (sqlite3_prepare_v2(database, sql.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        std::cout << "getTopic prepare failed: " << sqlite3_errmsg(database) << std::endl;
        return topic;
    }

    sqlite3_bind_int(stmt, 1, topicID);

    if (sqlite3_step(stmt) == SQLITE_ROW) {
        topic = rowToTopic(stmt);
    }

    sqlite3_finalize(stmt);
    return topic;
}

std::string getTopicContents(sqlite3* database, int user_id, int topicID) {
    Topic topic;
    sqlite3_stmt* stmt;
    const std::string sql = "SELECT * FROM TOPIC WHERE ID=?";

    if (sqlite3_prepare_v2(database, sql.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        std::cout << "getTopicContents prepare failed: " << sqlite3_errmsg(database) << std::endl;
        return "";
    }

    sqlite3_bind_int(stmt, 1, topicID);

    if (sqlite3_step(stmt) == SQLITE_ROW) {
        topic = rowToTopic(stmt);
    } else {
        std::cout << "Topic " << topicID << " not found." << std::endl;
        sqlite3_finalize(stmt);
        return "";
    }

    sqlite3_finalize(stmt);

    std::string fileName = userTopicsDir(user_id) + "/" + std::to_string(topic.id) + "_" + sanitizeFileName(topic.topicTitle) + ".md";
    std::ifstream topicFile(fileName);
    if (!topicFile.is_open()) {
        std::cout << "Failed to open topic file: " << fileName << std::endl;
        return "";
    }

    std::string content((std::istreambuf_iterator<char>(topicFile)),
                        std::istreambuf_iterator<char>());
    topicFile.close();
    return content;
}

void replaceTopicContents(sqlite3* database, int user_id, int topicID, const std::string& newContent) {
    Topic topic;
    sqlite3_stmt* stmt;
    const std::string sql = "SELECT * FROM TOPIC WHERE ID=?";

    if (sqlite3_prepare_v2(database, sql.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        std::cout << "replaceTopicContents prepare failed: " << sqlite3_errmsg(database) << std::endl;
        return;
    }

    sqlite3_bind_int(stmt, 1, topicID);

    if (sqlite3_step(stmt) == SQLITE_ROW) {
        topic = rowToTopic(stmt);
    } else {
        std::cout << "Topic " << topicID << " not found." << std::endl;
        sqlite3_finalize(stmt);
        return;
    }

    sqlite3_finalize(stmt);

    std::string fileName = userTopicsDir(user_id) + "/" + std::to_string(topic.id) + "_" + sanitizeFileName(topic.topicTitle) + ".md";
    std::ofstream topicFile(fileName);
    if (!topicFile.is_open()) {
        std::cout << "Failed to open topic file for writing: " << fileName << std::endl;
        return;
    }

    topicFile << newContent;
    topicFile.close();
}

EditResult editTopicContents(sqlite3* database, int user_id, int topicID, const std::vector<EditPatch>& patches) {
    EditResult result;
    Topic topic;
    sqlite3_stmt* stmt;
    const std::string sql = "SELECT * FROM TOPIC WHERE ID=?";

    if (sqlite3_prepare_v2(database, sql.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        result.ok = false;
        result.error = "DB prepare failed";
        return result;
    }
    sqlite3_bind_int(stmt, 1, topicID);

    if (sqlite3_step(stmt) == SQLITE_ROW) {
        topic = rowToTopic(stmt);
    } else {
        sqlite3_finalize(stmt);
        result.ok = false;
        result.error = "Topic not found.";
        return result;
    }
    sqlite3_finalize(stmt);

    std::string fileName = userTopicsDir(user_id) + "/" + std::to_string(topic.id) + "_" + sanitizeFileName(topic.topicTitle) + ".md";
    std::ifstream in(fileName);
    if (!in.is_open()) {
        result.ok = false;
        result.error = "Failed to open topic file: " + fileName;
        return result;
    }
    std::string content((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
    in.close();

    for (size_t i = 0; i < patches.size(); ++i) {
        const EditPatch& p = patches[i];
        if (p.find.empty()) {
            result.ok = false;
            result.error = "Patch " + std::to_string(i) + ": 'find' string is empty.";
            return result;
        }

        std::vector<size_t> positions;
        size_t pos = 0;
        while ((pos = content.find(p.find, pos)) != std::string::npos) {
            positions.push_back(pos);
            pos += p.find.size();
        }

        if (positions.empty()) {
            result.ok = false;
            result.error = "Patch " + std::to_string(i) + ": 'find' string not found. Verify exact text including whitespace and newlines.";
            return result;
        }

        size_t targetPos;
        if (p.occurrence > 0) {
            if (static_cast<size_t>(p.occurrence) > positions.size()) {
                result.ok = false;
                result.error = "Patch " + std::to_string(i) + ": requested occurrence " +
                               std::to_string(p.occurrence) + " but only " +
                               std::to_string(positions.size()) + " match(es) found.";
                return result;
            }
            targetPos = positions[p.occurrence - 1];
        } else {
            if (positions.size() > 1) {
                result.ok = false;
                result.error = "Patch " + std::to_string(i) + ": 'find' string matched " +
                               std::to_string(positions.size()) +
                               " times. Widen 'find' with surrounding context to make it unique, or set 'occurrence'.";
                return result;
            }
            targetPos = positions[0];
        }

        content.replace(targetPos, p.find.size(), p.replace);
        result.applied++;
    }

    std::ofstream out(fileName);
    if (!out.is_open()) {
        result.ok = false;
        result.error = "Failed to open topic file for writing: " + fileName;
        return result;
    }
    out << content;
    out.close();

    return result;
}

std::vector<Topic> filterTopics(sqlite3* database, const TopicFilterOptions& opts) {
    std::string sql = "SELECT * FROM TOPIC";
    std::vector<std::string> conditions;
    std::vector<std::string> params;

    // Category match is case-insensitive (ASCII) via COLLATE NOCASE, mirroring
    // the event filter.
    if (!opts.category.empty()) {
        conditions.push_back("TOPIC_CATEGORY = ? COLLATE NOCASE");
        params.push_back(opts.category);
    } else if (!opts.categories.empty()) {
        std::string inClause = "TOPIC_CATEGORY IN (";
        for (size_t i = 0; i < opts.categories.size(); i++) {
            inClause += (i == 0 ? "?" : ",?");
            params.push_back(opts.categories[i]);
        }
        inClause += ") COLLATE NOCASE";
        conditions.push_back(inClause);
    }

    if (!opts.keyword.empty()) {
        conditions.push_back("(TOPIC_TITLE LIKE ? OR TOPIC_SUMMARY LIKE ?)");
        std::string like = "%" + opts.keyword + "%";
        params.push_back(like);
        params.push_back(like);
    }

    if (!conditions.empty()) {
        sql += " WHERE ";
        for (size_t i = 0; i < conditions.size(); i++) {
            if (i > 0) sql += " AND ";
            sql += conditions[i];
        }
    }

    sqlite3_stmt* stmt;
    if (sqlite3_prepare_v2(database, sql.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        std::cout << "filterTopics prepare failed: " << sqlite3_errmsg(database) << std::endl;
        return {};
    }

    for (size_t i = 0; i < params.size(); i++) {
        sqlite3_bind_text(stmt, static_cast<int>(i + 1), params[i].c_str(), -1, SQLITE_TRANSIENT);
    }

    std::vector<Topic> results;
    while (sqlite3_step(stmt) == SQLITE_ROW) {
        results.push_back(rowToTopic(stmt));
    }
    sqlite3_finalize(stmt);

    if (opts.limit > 0 && static_cast<int>(results.size()) > opts.limit) {
        results.resize(opts.limit);
    }

    return results;
}

// ─── Convenience topic filter functions ──────────────────────────────────────

std::vector<Topic> getTopicsByCategory(sqlite3* database, const std::string& category) {
    TopicFilterOptions opts;
    opts.category = category;
    return filterTopics(database, opts);
}

std::vector<Topic> searchTopics(sqlite3* database, const std::string& keyword) {
    TopicFilterOptions opts;
    opts.keyword = keyword;
    return filterTopics(database, opts);
}

// ─── JSON serialization / deserialization ────────────────────────────────────

static crow::json::wvalue topicToJson(const Topic& t) {
    crow::json::wvalue j;
    j["id"]       = t.id;
    j["title"]    = t.topicTitle;
    j["summary"]  = t.topicSummary;
    j["category"] = t.topicCategory;
    j["folder"]   = t.topicFolder;
    return j;
}

static crow::json::wvalue eventToJson(const CalenderEvent& e) {
    crow::json::wvalue j;
    j["id"]       = e.id;
    j["title"]    = e.eventTitle;
    j["summary"]  = e.eventSummary;
    j["category"] = e.eventCategory;
    j["date"]     = e.eventDate;
    j["time"]     = e.eventTime;
    j["end_date"] = e.eventEndDate;
    j["end_time"] = e.eventEndTime;
    j["archived"] = e.eventArchived;
    j["commitment_id"]    = e.commitmentId;
    j["status"]           = e.status.empty() ? "scheduled" : e.status;
    j["status_change_reason"] = e.statusChangeReason;
    j["rescheduled_from"] = e.rescheduledFrom;
    j["created_at"]       = e.createdAt;
    j["last_modified_at"] = e.lastModifiedAt;
    return j;
}

static FilterOptions parseFilterOptions(const crow::json::rvalue& j) {
    FilterOptions opts;

    // Parse date filter.
    if (j.has("filter_by_date"))
        opts.filterByDate = j["filter_by_date"].b();
    if (j.has("start_date"))
        opts.startDate = parseDate(std::string(j["start_date"].s()));
    if (j.has("end_date"))
        opts.endDate = parseDate(std::string(j["end_date"].s()));

    // Parse time filter.
    if (j.has("filter_by_time"))
        opts.filterByTime = j["filter_by_time"].b();
    if (j.has("start_time"))
        opts.startTime = parseTime(std::string(j["start_time"].s()));
    if (j.has("end_time"))
        opts.endTime = parseTime(std::string(j["end_time"].s()));

    // Parse categories. These 2 should be combined into one.
    if (j.has("category"))
        opts.category = std::string(j["category"].s());
    if (j.has("categories")) {
        for (size_t i = 0; i < j["categories"].size(); ++i)
            opts.categories.push_back(std::string(j["categories"][i].s()));
    }

    if (j.has("keyword"))
        opts.keyword = std::string(j["keyword"].s());

    if (j.has("archived")) {
        std::string arc = std::string(j["archived"].s());
        if (arc == "all")
            opts.archived = FilterOptions::ArchivedFilter::All;
        else if (arc == "archived_only")
            opts.archived = FilterOptions::ArchivedFilter::ArchivedOnly;
        else if (arc == "active_only")
            opts.archived = FilterOptions::ArchivedFilter::ActiveOnly;
    }

    if (j.has("sort_order")) {
        if (std::string(j["sort_order"].s()) == "desc")
            opts.sortOrder = FilterOptions::SortOrder::DateDesc;
        if (std::string(j["sort_order"].s()) == "asc")
            opts.sortOrder = FilterOptions::SortOrder::DateAsc;
    }

    if (j.has("limit"))
        opts.limit = static_cast<int>(j["limit"].i());

    return opts;
}

// Parses an event from a create/replace request. `base` is the starting record:
// fields absent from the JSON keep their base value. On an edit, pass the existing
// event as base so omitting the optional commitment fields preserves them rather
// than resetting them to defaults; on create, the default-constructed base applies.
static CalenderEvent parseEditEvent(const crow::json::rvalue& j,
                                    const CalenderEvent& base = CalenderEvent()) {
    CalenderEvent event = base;
    if (j.has("id"))       event.id            = static_cast<int>(j["id"].i());
    if (j.has("title"))    event.eventTitle    = std::string(j["title"].s());
    if (j.has("summary"))  event.eventSummary  = std::string(j["summary"].s());
    if (j.has("category")) event.eventCategory = std::string(j["category"].s());
    if (j.has("date"))     event.eventDate     = std::string(j["date"].s());
    if (j.has("time"))     event.eventTime     = std::string(j["time"].s());
    if (j.has("end_date")) event.eventEndDate  = std::string(j["end_date"].s());
    if (j.has("end_time")) event.eventEndTime  = std::string(j["end_time"].s());
    if (j.has("archived")) event.eventArchived = j["archived"].b();
    if (j.has("commitment_id"))    event.commitmentId    = std::string(j["commitment_id"].s());
    if (j.has("status"))           event.status          = std::string(j["status"].s());
    if (j.has("status_change_reason")) event.statusChangeReason = std::string(j["status_change_reason"].s());
    if (j.has("rescheduled_from")) event.rescheduledFrom = std::string(j["rescheduled_from"].s());
    return event;
}

static Topic parseEditTopic(const crow::json::rvalue& j) {
    Topic topic;
    if (j.has("id"))       topic.id            = static_cast<int>(j["id"].i());
    if (j.has("title"))    topic.topicTitle    = std::string(j["title"].s());
    if (j.has("summary"))  topic.topicSummary  = std::string(j["summary"].s());
    if (j.has("category")) topic.topicCategory = std::string(j["category"].s());
    if (j.has("folder"))   topic.topicFolder   = std::string(j["folder"].s());
    return topic;
}

// Trim leading/trailing whitespace. Used to validate folder names so callers
// can't sneak an effectively-empty name like "  " past the non-empty check.
static std::string trimWhitespace(const std::string& s) {
    size_t start = 0;
    while (start < s.size() && std::isspace(static_cast<unsigned char>(s[start]))) ++start;
    size_t end = s.size();
    while (end > start && std::isspace(static_cast<unsigned char>(s[end - 1]))) --end;
    return s.substr(start, end - start);
}

// Folder names are user-facing labels — keep them short and printable. Returns
// "" on success, or a human-readable error reason otherwise. Empty input is
// considered valid here (callers decide whether "" means "root" or is itself
// an error — rename_folder rejects empty, create/replace allow it).
static const size_t FOLDER_NAME_MAX_BYTES = 32;
static std::string validateFolderName(const std::string& name) {
    if (name.empty()) return "";
    if (name.size() > FOLDER_NAME_MAX_BYTES) {
        return "Folder name is too long (max " +
               std::to_string(FOLDER_NAME_MAX_BYTES) + " bytes).";
    }
    // Reject control chars (incl. tab/newline) and the UTF-8 encoding of the
    // Unicode replacement character U+FFFD (EF BF BD) which signals upstream
    // encoding corruption — storing it cements the corruption.
    for (size_t i = 0; i < name.size(); ++i) {
        unsigned char c = static_cast<unsigned char>(name[i]);
        if (c < 0x20 || c == 0x7F) {
            return "Folder name contains a control character.";
        }
        if (c == 0xEF && i + 2 < name.size() &&
            static_cast<unsigned char>(name[i + 1]) == 0xBF &&
            static_cast<unsigned char>(name[i + 2]) == 0xBD) {
            return "Folder name contains the Unicode replacement character (U+FFFD).";
        }
    }
    return "";
}

// Look up the canonical (first-seen) casing for a folder name by scanning
// TOPIC_FOLDER values case-insensitively. If any topic already uses a
// case-variant of `name`, that variant is returned so a second caller passing
// "testfolder" doesn't fork from an existing "TestFolder". If no match, the
// input is returned unchanged so the first user of a new folder defines its
// casing.
static std::string canonicalFolderName(sqlite3* database, const std::string& name) {
    if (name.empty()) return name;
    sqlite3_stmt* stmt;
    const std::string sql =
        "SELECT TOPIC_FOLDER FROM TOPIC "
        "WHERE LOWER(TOPIC_FOLDER) = LOWER(?) AND TOPIC_FOLDER != '' "
        "LIMIT 1";
    if (sqlite3_prepare_v2(database, sql.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        return name;
    }
    sqlite3_bind_text(stmt, 1, name.c_str(), -1, SQLITE_STATIC);
    std::string canonical = name;
    if (sqlite3_step(stmt) == SQLITE_ROW) {
        const unsigned char* col = sqlite3_column_text(stmt, 0);
        if (col) canonical = reinterpret_cast<const char*>(col);
    }
    sqlite3_finalize(stmt);
    return canonical;
}

// Bulk-rename TOPIC_FOLDER values. Returns number of rows updated, or -1 on
// SQL error. The replace_topic flow handles the single-topic case.
int renameFolder(sqlite3* database, const std::string& oldName, const std::string& newName) {
    sqlite3_stmt* stmt;
    // Match case-insensitively on the old name so "TestFolder" and
    // "testfolder" rename together — callers no longer need to know the
    // exact casing stored on disk.
    const std::string sql =
        "UPDATE TOPIC SET TOPIC_FOLDER=? WHERE LOWER(TOPIC_FOLDER)=LOWER(?)";
    if (sqlite3_prepare_v2(database, sql.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
        std::cout << "renameFolder prepare failed: " << sqlite3_errmsg(database) << std::endl;
        return -1;
    }
    sqlite3_bind_text(stmt, 1, newName.c_str(), -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 2, oldName.c_str(), -1, SQLITE_STATIC);
    int rc = sqlite3_step(stmt);
    sqlite3_finalize(stmt);
    if (rc != SQLITE_DONE) return -1;
    return sqlite3_changes(database);
}

static TopicFilterOptions parseTopicFilterOptions(const crow::json::rvalue& j) {
    TopicFilterOptions opts;
    if (j.has("category")) {
        opts.category = std::string(j["category"].s());
    }
    if (j.has("categories")) {
        for (size_t i = 0; i < j["categories"].size(); ++i)
            opts.categories.push_back(std::string(j["categories"][i].s()));
    }
    if (j.has("keyword"))
        opts.keyword = std::string(j["keyword"].s());
    if (j.has("limit"))
        opts.limit = static_cast<int>(j["limit"].i());
    return opts;
}

// ─── Per-user DB resolution ──────────────────────────────────────────────────

// Returns the sqlite handle for `user_id`, opening and initializing it on
// first use. Always returns a non-null handle (sqlite3_open creates the file
// if it's missing). Thread-safe: the mutex serializes lookup/insertion only;
// the returned handle is then used lock-free by the caller, which is fine
// because sqlite is built in SERIALIZED mode.
static sqlite3* getUserDb(int user_id) {
    std::lock_guard<std::mutex> lock(g_user_dbs_mu);
    auto it = g_user_dbs.find(user_id);
    if (it != g_user_dbs.end()) {
        return it->second;
    }
    std::filesystem::create_directories(userDir(user_id));
    std::string dbPath = userDir(user_id) + "/database.sql";
    sqlite3* db = nullptr;
    openDatabase(const_cast<char*>(dbPath.c_str()), &db);
    createCalender(db);
    createTopics(db);
    g_user_dbs.emplace(user_id, db);
    return db;
}

// Drop a user's cached sqlite handle so the next request re-opens
// database.sql from disk. Called via the "reload_database" API action after
// the web frontend replaces a user's database during a data import. The
// caller must ensure no other request for this user is in flight — the import
// flow is a single, deliberate user action — since handed-out handles are
// used lock-free.
static void eraseUserDb(int user_id) {
    std::lock_guard<std::mutex> lock(g_user_dbs_mu);
    auto it = g_user_dbs.find(user_id);
    if (it != g_user_dbs.end()) {
        sqlite3_close(it->second);
        g_user_dbs.erase(it);
    }
}

// Move a legacy single-user layout under <root>/database.sql + <root>/topics/
// to user 1 so existing data keeps working after the multi-user switch.
// Idempotent: if user 1 already has data, the legacy files are left alone
// rather than overwritten (we never want to clobber an existing user 1).
static void migrateLegacyIfPresent() {
    std::string legacyDb = g_root + "/database.sql";
    std::string legacyTopics = g_root + "/topics";
    std::string user1Db = userDir(1) + "/database.sql";
    if (!std::filesystem::exists(legacyDb)) return;
    if (std::filesystem::exists(user1Db)) {
        std::cout << "Legacy DB present at " << legacyDb
                  << " but user 1 already initialized — leaving legacy in place."
                  << std::endl;
        return;
    }
    std::filesystem::create_directories(userDir(1));
    std::error_code ec;
    std::filesystem::rename(legacyDb, user1Db, ec);
    if (ec) {
        std::cout << "Legacy migration: failed to move " << legacyDb << " → "
                  << user1Db << ": " << ec.message() << std::endl;
        return;
    }
    if (std::filesystem::exists(legacyTopics)) {
        std::filesystem::rename(legacyTopics, userTopicsDir(1), ec);
        if (ec) {
            std::cout << "Legacy migration: failed to move topics dir: "
                      << ec.message() << std::endl;
        }
    }
    std::cout << "Migrated legacy database to user 1." << std::endl;
}

// Extract and validate user_id from the request body. Returns true and sets
// `out` on success; on failure populates `err` with an HTTP response.
static bool requireUserId(const crow::json::rvalue& j, int& out, crow::response& err) {
    if (!j.has("user_id")) {
        err = crow::response(400, "missing required field: user_id");
        return false;
    }
    int64_t v = 0;
    try {
        v = j["user_id"].i();
    } catch (...) {
        err = crow::response(400, "user_id must be an integer");
        return false;
    }
    // Positive only: 0 / negative would map to no real account and could
    // collide with the sentinel returned by getEvent/getTopic for "not found".
    if (v <= 0 || v > 2147483647) {
        err = crow::response(400, "user_id out of range");
        return false;
    }
    out = static_cast<int>(v);
    return true;
}

// ─── main ─────────────────────────────────────────────────────────────────────

int main(int argc, char* argv[]) {
    const char* home = std::getenv("HOME");
    g_root = (home ? std::string(home) : std::string("")) +
             "/.local/share/aime-assistant/database";
    if (argc > 1) {
        g_root = argv[1];
    }
    std::filesystem::create_directories(g_root + "/users");
    migrateLegacyIfPresent();

    crow::SimpleApp DatabaseAPI;
    CROW_ROUTE(DatabaseAPI, "/api").methods("POST"_method)([](const crow::request& req){
        auto jsonData = crow::json::load(req.body);
        if (!jsonData) {
            return crow::response(400, "Invalid JSON");
        }

        if (!jsonData.has("tool_name")) {
            return crow::response(400, "Invalid JSON");
        }

        int user_id = 0;
        crow::response authErr;
        if (!requireUserId(jsonData, user_id, authErr)) {
            return authErr;
        }
        // Maintenance action: drop this user's cached sqlite handle so the
        // next request re-opens database.sql from disk. The web frontend
        // calls this after replacing a user's database during a data import,
        // so the backend stops writing to the stale (pre-import) handle.
        if (jsonData["tool_name"] == "reload_database") {
            eraseUserDb(user_id);
            crow::json::wvalue response;
            response["ok"] = true;
            return crow::response(200, response);
        }

        sqlite3* database = getUserDb(user_id);

        if (jsonData["tool_name"] == "get_events") {
            // Reconcile stale past events before reading so every surface
            // (calendar, model, pattern tools) sees a coherent, up-to-date
            // store. `now_*` arrive in the user's local time; absent them we
            // skip rather than guess with the server's UTC clock.
            if (jsonData.has("now_date")) {
                reconcileStalePastEvents(
                    database,
                    std::string(jsonData["now_date"].s()),
                    jsonData.has("now_time") ? std::string(jsonData["now_time"].s())
                                             : std::string());
            }
            FilterOptions opts = parseFilterOptions(jsonData);
            std::vector<CalenderEvent> events = filterEvents(database, opts);

            crow::json::wvalue response;
            response["count"] = static_cast<int>(events.size());
            for (size_t i = 0; i < events.size(); ++i) {
                response["events"][i] = eventToJson(events[i]);
            }

            return crow::response(200, response);
        } else if (jsonData["tool_name"] == "replace_event") {
            if (!jsonData.has("id") || !jsonData.has("title") || !jsonData.has("summary") ||
                !jsonData.has("category") || !jsonData.has("date") ||
                !jsonData.has("archived")) {
                return crow::response(400, "replace_event missing required fields");
            }
            if (jsonData.has("status")) {
                std::string st = std::string(jsonData["status"].s());
                if (!st.empty() && !isValidStatus(st)) {
                    return crow::response(400,
                        "Invalid status \"" + st + "\". Valid statuses: "
                        "scheduled, completed, canceled. ('unknown' is set "
                        "automatically by the system for elapsed events — "
                        "don't set it yourself.)");
                }
            }

            int editId = static_cast<int>(jsonData["id"].i());
            CalenderEvent existing = getEvent(database, editId);
            if (existing.id == -1) {
                crow::json::wvalue response;
                response["ok"] = false;
                response["error"] = "Event not found. Use create_event to add a new event.";
                response["id"] = editId;
                return crow::response(404, response);
            }

            // Merge onto the existing record so omitted optional fields (notably
            // the commitment-tracking ones) are preserved rather than reset.
            CalenderEvent event = parseEditEvent(jsonData, existing);

            updateEvent(database, event);
            if (event.eventArchived)
                archiveEvent(database, event.id);
            else
                unarchiveEvent(database, event.id);

            crow::json::wvalue response;
            response["ok"] = true;
            response["id"] = event.id;
            return crow::response(200, response);
        } else if (jsonData["tool_name"] == "create_event") {
            if (!jsonData.has("title") || !jsonData.has("summary") ||
                !jsonData.has("category") || !jsonData.has("date") ||
                !jsonData.has("archived")) {
                return crow::response(400, "create_event missing required fields");
            }
            if (jsonData.has("status")) {
                std::string st = std::string(jsonData["status"].s());
                if (!st.empty() && !isValidStatus(st)) {
                    return crow::response(400,
                        "Invalid status \"" + st + "\". Valid statuses: "
                        "scheduled, completed, canceled. ('unknown' is set "
                        "automatically by the system for elapsed events — "
                        "don't set it yourself.)");
                }
            }

            CalenderEvent event = parseEditEvent(jsonData);
            event.id = -1;

            addEvent(database, event);

            if (event.eventArchived)
                archiveEvent(database, event.id);

            crow::json::wvalue response;
            response["ok"] = true;
            response["id"] = event.id;
            return crow::response(200, response);
        } else if (jsonData["tool_name"] == "get_topics") {
            TopicFilterOptions opts = parseTopicFilterOptions(jsonData);
            std::vector<Topic> topics = filterTopics(database, opts);

            crow::json::wvalue response;
            response["count"] = static_cast<int>(topics.size());
            for (size_t i = 0; i < topics.size(); ++i) {
                response["topics"][i] = topicToJson(topics[i]);
            }

            return crow::response(200, response);
        } else if (jsonData["tool_name"] == "create_topic") {
            if (!jsonData.has("title") || !jsonData.has("summary") ||
                !jsonData.has("category")) {
                return crow::response(400, "create_topic missing required fields");
            }

            Topic topic = parseEditTopic(jsonData);
            topic.id = -1;
            topic.topicFolder = trimWhitespace(topic.topicFolder);
            {
                std::string err = validateFolderName(topic.topicFolder);
                if (!err.empty()) {
                    crow::json::wvalue response;
                    response["ok"] = false;
                    response["error"] = err;
                    return crow::response(400, response);
                }
            }
            topic.topicFolder = canonicalFolderName(database, topic.topicFolder);

            addTopic(database, user_id, topic);

            crow::json::wvalue response;
            response["ok"] = true;
            response["id"] = topic.id;
            return crow::response(200, response);
        } else if (jsonData["tool_name"] == "replace_topic") {
            if (!jsonData.has("id") || !jsonData.has("summary") || !jsonData.has("category")) {
                return crow::response(400, "replace_topic missing required fields: id, summary, category");
            }

            Topic topic = parseEditTopic(jsonData);

            Topic existing = getTopic(database, topic.id);
            if (existing.id == -1) {
                crow::json::wvalue response;
                response["ok"] = false;
                response["error"] = "Topic not found. Use create_topic to add a new topic.";
                response["id"] = topic.id;
                return crow::response(404, response);
            }

            // Preserve the title — title changes would require renaming the .md file.
            topic.topicTitle = existing.topicTitle;
            // If the caller didn't include a folder field, keep the existing one
            // so an edit that only changes summary/category doesn't drop the
            // topic back to the root by accident.
            if (!jsonData.has("folder")) {
                topic.topicFolder = existing.topicFolder;
            } else {
                // To clear a folder, callers send an empty string, so we
                // trim and accept that as "root". A whitespace-only string
                // is treated as empty.
                topic.topicFolder = trimWhitespace(topic.topicFolder);
                std::string err = validateFolderName(topic.topicFolder);
                if (!err.empty()) {
                    crow::json::wvalue response;
                    response["ok"] = false;
                    response["error"] = err;
                    return crow::response(400, response);
                }
                topic.topicFolder = canonicalFolderName(database, topic.topicFolder);
            }
            updateTopic(database, topic);

            crow::json::wvalue response;
            response["ok"] = true;
            response["id"] = topic.id;
            return crow::response(200, response);
        } else if (jsonData["tool_name"] == "get_topic_contents") {
            if (!jsonData.has("id")) {
                return crow::response(400, "get_topic_contents missing required field: id");
            }

            int topicID = static_cast<int>(jsonData["id"].i());
            std::string contents = getTopicContents(database, user_id, topicID);

            crow::json::wvalue response;
            response["id"] = topicID;
            response["contents"] = contents;
            return crow::response(200, response);
        } else if (jsonData["tool_name"] == "replace_topic_contents") {
            if (!jsonData.has("id") || !jsonData.has("contents")) {
                return crow::response(400, "replace_topic_contents missing required fields: id, contents");
            }

            int topicID = static_cast<int>(jsonData["id"].i());
            std::string newContents = std::string(jsonData["contents"].s());

            replaceTopicContents(database, user_id, topicID, newContents);

            crow::json::wvalue response;
            response["ok"] = true;
            response["id"] = topicID;
            return crow::response(200, response);
        } else if (jsonData["tool_name"] == "list_folders") {
            sqlite3_stmt* stmt;
            const std::string sql =
                "SELECT TOPIC_FOLDER, COUNT(*) FROM TOPIC "
                "WHERE TOPIC_FOLDER != '' "
                "GROUP BY TOPIC_FOLDER ORDER BY TOPIC_FOLDER";
            crow::json::wvalue response;
            if (sqlite3_prepare_v2(database, sql.c_str(), -1, &stmt, nullptr) != SQLITE_OK) {
                response["ok"] = false;
                response["error"] = "list_folders prepare failed.";
                return crow::response(500, response);
            }
            int i = 0;
            while (sqlite3_step(stmt) == SQLITE_ROW) {
                const unsigned char* nameRaw = sqlite3_column_text(stmt, 0);
                std::string name = nameRaw ? reinterpret_cast<const char*>(nameRaw) : "";
                int cnt = sqlite3_column_int(stmt, 1);
                response["folders"][i]["name"]  = name;
                response["folders"][i]["count"] = cnt;
                ++i;
            }
            sqlite3_finalize(stmt);
            response["count"] = i;
            return crow::response(200, response);
        } else if (jsonData["tool_name"] == "rename_folder") {
            if (!jsonData.has("old_name") || !jsonData.has("new_name")) {
                return crow::response(400, "rename_folder missing required fields: old_name, new_name");
            }
            std::string oldName = trimWhitespace(std::string(jsonData["old_name"].s()));
            std::string newName = trimWhitespace(std::string(jsonData["new_name"].s()));
            if (oldName.empty() || newName.empty()) {
                crow::json::wvalue response;
                response["ok"] = false;
                response["error"] = "Folder names must be non-empty.";
                return crow::response(400, response);
            }
            {
                std::string err = validateFolderName(newName);
                if (!err.empty()) {
                    crow::json::wvalue response;
                    response["ok"] = false;
                    response["error"] = err;
                    return crow::response(400, response);
                }
            }
            // If the new name is just a case-variant of an existing folder,
            // align to that folder's canonical casing so the rename merges
            // cleanly instead of creating yet another near-duplicate.
            newName = canonicalFolderName(database, newName);
            int updated = renameFolder(database, oldName, newName);
            crow::json::wvalue response;
            if (updated < 0) {
                response["ok"] = false;
                response["error"] = "rename_folder failed.";
                return crow::response(500, response);
            }
            response["ok"] = true;
            response["updated"] = updated;
            return crow::response(200, response);
        } else if (jsonData["tool_name"] == "edit_topic_contents") {
            if (!jsonData.has("id") || !jsonData.has("patches")) {
                return crow::response(400, "edit_topic_contents missing required fields: id, patches");
            }

            int topicID = static_cast<int>(jsonData["id"].i());

            std::vector<EditPatch> patches;
            auto patchesNode = jsonData["patches"];
            for (size_t i = 0; i < patchesNode.size(); ++i) {
                EditPatch p;
                if (!patchesNode[i].has("find") || !patchesNode[i].has("replace")) {
                    crow::json::wvalue response;
                    response["ok"] = false;
                    response["id"] = topicID;
                    response["error"] = "Patch " + std::to_string(i) + " missing required 'find' or 'replace'.";
                    return crow::response(400, response);
                }
                p.find    = std::string(patchesNode[i]["find"].s());
                p.replace = std::string(patchesNode[i]["replace"].s());
                if (patchesNode[i].has("occurrence")) {
                    p.occurrence = static_cast<int>(patchesNode[i]["occurrence"].i());
                }
                patches.push_back(p);
            }

            EditResult er = editTopicContents(database, user_id, topicID, patches);

            crow::json::wvalue response;
            response["ok"] = er.ok;
            response["id"] = topicID;
            response["applied"] = er.applied;
            if (!er.ok) {
                response["error"] = er.error;
                return crow::response(400, response);
            }
            return crow::response(200, response);
        }

        return crow::response(400, "Invalid Tool Name");
    });

    DatabaseAPI.bindaddr("127.0.0.1").port(8080).multithreaded().run();

    // Close every per-user handle on shutdown. Hold the mutex even though
    // workers should already be stopped at this point — defensive.
    {
        std::lock_guard<std::mutex> lock(g_user_dbs_mu);
        for (auto& kv : g_user_dbs) sqlite3_close(kv.second);
        g_user_dbs.clear();
    }
    return 0;
}
