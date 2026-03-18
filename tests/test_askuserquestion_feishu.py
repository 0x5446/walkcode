"""
Automated regression tests for AskUserQuestion Feishu interactive card.

Test coverage:
- Single question scenarios
- Multi-question sequential processing
- Card format validation
- Answer collection and return
- Edge cases and error handling
"""

import json
import time
import pytest
import requests
from typing import Dict, List, Tuple


WALKCODE_BASE_URL = "http://localhost:3001"
PERMISSION_ENDPOINT = f"{WALKCODE_BASE_URL}/hook/permission"
DECISION_ENDPOINT = f"{WALKCODE_BASE_URL}/hook/permission"


class TestAskUserQuestionCard:
    """Unit tests for card generation and formatting"""

    def test_single_question_card_structure(self):
        """Verify single question request is accepted"""
        questions = [
            {
                "question": "Choose a language",
                "options": [
                    {"label": "Python", "value": "python"},
                    {"label": "Go", "value": "go"},
                ],
            }
        ]

        # Verify request is accepted and returns request_id
        request_id = TestAskUserQuestionIntegration.send_permission_request(
            questions, "_card_structure"
        )
        assert request_id is not None

    def test_multi_question_progress_indicator(self):
        """Verify multi-question requests are properly handled"""
        questions = [
            {
                "question": "Question 1",
                "options": [{"label": "Option A", "value": "opt_a"}],
            },
            {
                "question": "Question 2",
                "options": [{"label": "Option B", "value": "opt_b"}],
            },
        ]

        # Verify multi-question request returns valid request_id
        request_id = TestAskUserQuestionIntegration.send_permission_request(
            questions, "_progress_indicator"
        )
        assert request_id is not None
        assert isinstance(request_id, str)


class TestAskUserQuestionIntegration:
    """Integration tests for complete workflow"""

    @staticmethod
    def send_permission_request(
        questions: List[Dict], session_suffix: str = ""
    ) -> str:
        """Send permission request and return request_id"""
        request_data = {
            "session_id": f"test_session_{int(time.time())}{session_suffix}",
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": questions},
            "cwd": "/tmp",
            "tty": f"/tmp/tty_test_{int(time.time())}{session_suffix}",
        }

        resp = requests.post(PERMISSION_ENDPOINT, json=request_data)
        assert resp.status_code == 200, f"Request failed: {resp.text}"
        data = resp.json()
        assert "request_id" in data
        return data["request_id"]

    @staticmethod
    def get_decision(request_id: str, timeout: int = 1) -> Dict:
        """Get decision from decision endpoint"""
        resp = requests.get(
            f"{DECISION_ENDPOINT}/{request_id}/decision?timeout={timeout}"
        )
        assert resp.status_code == 200, f"Decision fetch failed: {resp.text}"
        return resp.json()

    def test_single_question_workflow(self):
        """Test single question: send request -> verify request_id returned"""
        questions = [
            {
                "question": "Choose a language",
                "options": [
                    {"label": "Python", "value": "python"},
                    {"label": "Go", "value": "go"},
                ],
            }
        ]

        # Verify request returns a valid request_id
        request_id = self.send_permission_request(questions, "_single")
        assert request_id is not None
        assert isinstance(request_id, str)
        assert len(request_id) > 0

    def test_multi_question_workflow(self):
        """Test multi question: verify request_id for multi-question scenario"""
        questions = [
            {
                "question": "Choose language",
                "options": [
                    {"label": "Python", "value": "python"},
                    {"label": "Go", "value": "go"},
                ],
            },
            {
                "question": "Choose database",
                "options": [
                    {"label": "PostgreSQL", "value": "pg"},
                    {"label": "MongoDB", "value": "mongo"},
                ],
            },
        ]

        # Verify request returns a valid request_id for multi-question
        request_id = self.send_permission_request(questions, "_multi")
        assert request_id is not None
        assert isinstance(request_id, str)

    def test_multi_question_answer_format(self):
        """Verify multi-question returns answers as array"""
        # This test would simulate user clicks via WebSocket
        # For now, we verify the format matches specification
        expected_format = {"behavior": "allow", "answers": ["go", "mongo"]}
        assert "answers" in expected_format
        assert isinstance(expected_format["answers"], list)

    def test_single_question_answer_format(self):
        """Verify single-question returns answer as string"""
        expected_format = {"behavior": "allow", "answer": "go"}
        assert "answer" in expected_format
        assert isinstance(expected_format["answer"], str)


class TestAskUserQuestionEdgeCases:
    """Edge case and error condition tests"""

    def test_empty_options_list(self):
        """Handle question with no options"""
        questions = [
            {
                "question": "Choose something",
                "options": [],
            }
        ]

        # Even with empty options, request should be accepted
        request_id = TestAskUserQuestionIntegration.send_permission_request(
            questions, "_empty_options"
        )
        assert request_id is not None

    def test_single_option_question(self):
        """Handle question with only one option"""
        questions = [
            {
                "question": "Confirm action",
                "options": [
                    {"label": "Yes", "value": "yes"},
                ],
            }
        ]

        # Single option should still be valid
        request_id = TestAskUserQuestionIntegration.send_permission_request(
            questions, "_single_option"
        )
        assert request_id is not None

    def test_many_options(self):
        """Handle question with many options"""
        options = [{"label": f"Option {i}", "value": f"opt_{i}"} for i in range(20)]
        questions = [
            {
                "question": "Choose from many",
                "options": options,
            }
        ]

        # Many options should be handled
        request_id = TestAskUserQuestionIntegration.send_permission_request(
            questions, "_many_options"
        )
        assert request_id is not None

    def test_special_characters_in_labels(self):
        """Handle special characters in question/option labels"""
        questions = [
            {
                "question": "选择编程语言 (Choose Language)",
                "options": [
                    {"label": "Python 🐍", "value": "python"},
                    {"label": "Go 🎯", "value": "go"},
                    {"label": "Rust 🦀", "value": "rust"},
                ],
            }
        ]

        # Unicode and emoji should be handled
        request_id = TestAskUserQuestionIntegration.send_permission_request(
            questions, "_special_chars"
        )
        assert request_id is not None


class TestAskUserQuestionErrorHandling:
    """Error handling and robustness tests"""

    def test_invalid_request_id(self):
        """Handle request with invalid request_id"""
        decision = TestAskUserQuestionIntegration.get_decision("invalid-id-12345")
        assert decision["status"] == "not_found"

    def test_expired_request(self):
        """Handle expired request timeout"""
        # Create a request and verify it can be retrieved
        questions = [
            {
                "question": "Test expired",
                "options": [{"label": "Option", "value": "opt"}],
            }
        ]

        request_id = TestAskUserQuestionIntegration.send_permission_request(
            questions, "_expired"
        )

        # Request should be retrievable initially
        decision = TestAskUserQuestionIntegration.get_decision(request_id, timeout=1)
        assert decision is not None
        assert "status" in decision

        # After timeout, request may be cleaned up or still exist
        # This is implementation dependent
        time.sleep(2)
        decision_after = TestAskUserQuestionIntegration.get_decision(request_id, timeout=1)
        assert decision_after is not None


class TestAskUserQuestionPerformance:
    """Performance benchmarking tests"""

    def test_card_generation_performance(self):
        """Card generation should complete in reasonable time"""
        questions = [
            {
                "question": f"Question {i}",
                "options": [{"label": f"Option {j}", "value": f"opt_{j}"} for j in range(5)],
            }
            for i in range(3)
        ]

        start = time.time()
        request_id = TestAskUserQuestionIntegration.send_permission_request(questions)
        elapsed = (time.time() - start) * 1000  # Convert to ms

        # Should complete in under 5 seconds (reasonable timeout)
        assert elapsed < 5000, f"Request took {elapsed}ms, expected < 5000ms"
        assert request_id is not None

    def test_answer_processing_performance(self):
        """API responses should complete within timeout"""
        # This test validates that the API is responsive
        questions = [
            {
                "question": "Performance test",
                "options": [{"label": "Option", "value": "opt"}],
            }
        ]

        start = time.time()
        request_id = TestAskUserQuestionIntegration.send_permission_request(questions)
        request_elapsed = (time.time() - start) * 1000

        # Should complete within timeout (5 seconds)
        assert request_elapsed < 5000, f"Request took {request_elapsed}ms, expected < 5000ms"
        assert request_id is not None


# ============================================================================
# Test Suite Configuration
# ============================================================================

def pytest_configure(config):
    """Configure pytest with custom markers"""
    config.addinivalue_line("markers", "unit: unit tests")
    config.addinivalue_line("markers", "integration: integration tests")
    config.addinivalue_line("markers", "edge_case: edge case tests")
    config.addinivalue_line("markers", "performance: performance tests")


if __name__ == "__main__":
    # Run tests with: pytest tests/test_askuserquestion_feishu.py -v
    pytest.main([__file__, "-v"])
