#!/usr/bin/env python3

import io
import json
import traceback
import uuid
import re
from contextlib import redirect_stdout, redirect_stderr
from typing import Dict, List, Any

from mcp.server.fastmcp import Context
from mcp.types import TextContent, ImageContent # Added ImageContent import

# Import the manager directly
from webEvalAgent.src.browser_manager import PlaywrightBrowserManager
# Only import run_browser_task from browser_utils
from webEvalAgent.src.browser_utils import run_browser_task, console_log_storage, network_request_storage
# Import your prompt function
from webEvalAgent.src.prompts import get_web_evaluation_prompt
# Import log server functions directly
from .log_server import send_log, start_log_server, open_log_dashboard, set_url_and_task
# For sleep
import asyncio
import time  # Ensure time is imported at the top level

# Constants for limiting output
MAX_ERROR_OUTPUT_CHARS = 10000  # Maximum characters to include in error output
MAX_TIMELINE_CHARS = 60000      # Maximum characters for the timeline section

# Function to get the singleton browser manager instance
def get_browser_manager() -> PlaywrightBrowserManager:
    """Get the singleton browser manager instance.
    
    This function provides a centralized way to access the singleton
    PlaywrightBrowserManager instance throughout the application.
    
    Returns:
        PlaywrightBrowserManager: The singleton browser manager instance
    """
    return PlaywrightBrowserManager.get_instance()

async def handle_web_evaluation(arguments: Dict[str, Any], ctx: Context, api_key: str) -> list[TextContent]:
    """Handle web_eval_agent tool calls
    
    This function evaluates the user experience of a web application by using
    the browser-use agent to perform specific tasks and analyze the interaction flow.
    
    Args:
        arguments: The tool arguments containing 'url' and 'task'
        ctx: The MCP context for reporting progress
        api_key: The API key for authentication with the LLM service
        
    Returns:
        list[List[Any]]: The evaluation results, including console logs, network requests, and screenshots
    """
    # Initialize log server immediately (if not already running)
    try:
        # stop_log_server() # Commented out stop_log_server
        start_log_server()
        # Give the server a moment to start
        await asyncio.sleep(1)
        # Open the dashboard in a new tab
        open_log_dashboard()
    except Exception as log_server_error:
        pass
    
    # Validate required arguments
    if "url" not in arguments or "task" not in arguments:
        return [TextContent(
            type="text",
            text="Error: Both 'url' and 'task' parameters are required. Please provide a URL to evaluate and a specific UX/UI task to test."
        )]
    
    url = arguments["url"]
    task = arguments["task"]
    tool_call_id = arguments.get("tool_call_id", str(uuid.uuid4()))
    headless = arguments.get("headless", True)
    
    if not url or not isinstance(url, str):
        return [TextContent(
            type="text",
            text="Error: 'url' must be a non-empty string containing the web application URL to evaluate."
        )]
        
    if not task or not isinstance(task, str):
        return [TextContent(
            type="text",
            text="Error: 'task' must be a non-empty string describing the UX/UI aspect to test."
        )]
    
    # Send initial status to dashboard
    send_log(f"🚀 Received web evaluation task: {task}", "🚀")
    send_log(f"🔗 Target URL: {url}", "🔗")
    
    # Update the URL and task in the dashboard
    set_url_and_task(url, task)

    # Get the singleton browser manager and initialize it
    browser_manager = get_browser_manager()
    if not browser_manager.is_initialized:
        # Note: browser_manager.initialize will no longer need to start the log server
        # since we've already done it above
        await browser_manager.initialize()
        
    # Get the evaluation task prompt
    evaluation_task = get_web_evaluation_prompt(url, task)
    send_log(f"📝 Generated evaluation prompt.", "📝")
    
    # Run the browser task
    agent_result_data = None # Changed to agent_result_data
    try:
        # run_browser_task now returns a dictionary with result and screenshots # Updated comment
        agent_result_data = await run_browser_task(
            evaluation_task,
            headless=headless, # Pass the headless parameter
            model="claude-3-7-sonnet-latest", # This model name might need update based on browser_utils
            ctx=ctx,
            tool_call_id=tool_call_id,
            api_key=api_key
        )
        
        # Extract the final result string
        agent_final_result = agent_result_data.get("result", "No result provided")
        screenshots = agent_result_data.get("screenshots", []) # Added this line

        # Log the number of screenshots captured
        send_log(f"📸 Captured {len(screenshots)} screenshots during evaluation", "📸")

        # Optional: Send the final result from the agent to the dashboard as well
        send_log(f"✅ Agent final result: {agent_final_result}", "✅")

    except Exception as browser_task_error:
        error_msg = f"Error during browser task execution: {browser_task_error}\n{traceback.format_exc()}"
        send_log(error_msg, "❌")
        agent_final_result = f"Error: {browser_task_error}" # Provide error as result
        screenshots = [] # Ensure screenshots is defined even on error

    # Format the agent result in a more user-friendly way, including console and network errors
    formatted_result = format_agent_result(agent_final_result, url, task, console_log_storage, network_request_storage)
    
    # Determine if the task was successful
    task_succeeded = True
    if agent_final_result.startswith("Error:"):
        task_succeeded = False
    elif "success=False" in agent_final_result and "is_done=True" in agent_final_result:
        task_succeeded = False
    
    # Use appropriate status emoji
    status_emoji = "✅" if task_succeeded else "❌"
    
    # Return a better formatted message to the MCP user
    # Including a reference to the dashboard for detailed logs
    confirmation_text = f"{formatted_result}\n\n👁️ See the 'Operative Control Center' dashboard for detailed live logs.\nWeb Evaluation completed!"
    send_log(f"Web evaluation task completed for {url}.", status_emoji) # Also send confirmation to dashboard
    # stop_log_server() # Commented out stop_log_server
    
    return [[TextContent(
        type="text",
        text=confirmation_text
    )] + [ImageContent(
        type="image",
        data=screenshot_data["screenshot"],
        mimeType="image/jpeg"
    ) for screenshot_data in screenshots]
    ]

def format_agent_result(result_str: str, url: str, task: str, console_logs=None, network_requests=None) -> str:
    """Format the agent result in a readable way with emojis.
    
    Args:
        result_str: Raw string representation of the agent result
        url: The URL that was evaluated
        task: The task that was executed
        console_logs: Collected console logs from the browser
        network_requests: Collected network requests from the browser
        
    Returns:
        str: Formatted result with steps and conclusion
    """
    # Start with a header
    formatted = f"📊 Web Evaluation Report for {url} complete!\n"
    formatted += f"📝 Completed Task: {task}\n\n"
    
    # Check if there's an error
    if result_str.startswith("Error:"):
        return f"{formatted}❌ {result_str}"
    
    # Flag to track if the task was successful
    task_succeeded = True
    
    # List to collect all agent steps with timestamps for the timeline
    agent_steps_timeline = []
    
    # Helper function for formatting error lists with character limit
    def format_error_list(items, item_formatter):
        """Format a list of error items with character limit.
        
        Args:
            items: List of error items to format
            item_formatter: Function that takes (index, item) and returns a formatted string
            
        Returns:
            str: Formatted error list with potential truncation
        """
        if not items:
            return " No items found.\n"
            
        result = f" ({len(items)} items)\n"
        
        # Combine all items with line breaks
        all_items_text = ""
        for i, item in enumerate(items):
            item_line = item_formatter(i, item)
            all_items_text += item_line
            
        # Truncate if necessary and add indicator
        if len(all_items_text) > MAX_ERROR_OUTPUT_CHARS:
            truncated_text = all_items_text[:MAX_ERROR_OUTPUT_CHARS]
            # Try to end at a newline if possible
            last_newline = truncated_text.rfind('\n')
            if last_newline > MAX_ERROR_OUTPUT_CHARS * 0.9:  # Only if we're not losing too much
                truncated_text = truncated_text[:last_newline+1]
                
            result += truncated_text
            result += f"  ... [Output truncated, {len(all_items_text) - len(truncated_text)} more characters not shown]\n"
        else:
            result += all_items_text
            
        return result
    
    # Try to extract action results from the string
    try:
        # Look for the all_results list in the string
        # This approach is more reliable than regex for simple extraction
        if "all_results=[" in result_str:
            # Get the part between all_results=[ and the next ]
            results_part = result_str.split("all_results=[")[1].split("]")[0]
            
            # Split by ActionResult to get individual results
            action_results = results_part.split("ActionResult(")
            
            # Skip the first empty item
            action_results = [r for r in action_results if r.strip()]
            
            # Check if the final action has success=False
            for action in action_results:
                if "is_done=True" in action:
                    if "success=False" in action:
                        task_succeeded = False
                    break
            
            # Format steps with emojis
            formatted += "🔍 Agent Steps:\n"
            
            # Approximate timestamps for steps - align with browser events rather than using current time
            # First, check if we have browser events to align with
            earliest_browser_time = None
            latest_browser_time = None
            
            # Get timeframe from console logs
            if console_logs:
                for log in console_logs:
                    timestamp = log.get('timestamp', 0)
                    if timestamp > 0:
                        if earliest_browser_time is None or timestamp < earliest_browser_time:
                            earliest_browser_time = timestamp
                        if latest_browser_time is None or timestamp > latest_browser_time:
                            latest_browser_time = timestamp
            
            # Check network requests too
            if network_requests:
                for req in network_requests:
                    timestamp = req.get('timestamp', 0)
                    if timestamp > 0:
                        if earliest_browser_time is None or timestamp < earliest_browser_time:
                            earliest_browser_time = timestamp
                        if latest_browser_time is None or timestamp > latest_browser_time:
                            latest_browser_time = timestamp
                    
                    # Also check response timestamp
                    resp_timestamp = req.get('response_timestamp', 0)
                    if resp_timestamp > 0:
                        if latest_browser_time is None or resp_timestamp > latest_browser_time:
                            latest_browser_time = resp_timestamp
            
            # Now set the agent step timings based on browser events
            current_time = time.time()
            
            # If we have browser events, position agent steps after the browser events
            # Otherwise, fall back to the current time approach
            if earliest_browser_time and latest_browser_time:
                # Position agent steps right after the browser events with a small gap (2 seconds)
                step_base_time = latest_browser_time + 2
                # Spread steps evenly over reasonable time period (5 sec per step)
                step_interval = 5
            else:
                # Fall back to current time approach but with a better baseline
                step_base_time = current_time - (len(action_results) * 5)
                step_interval = 5
            
            for i, action in enumerate(action_results):
                # Extract the extracted_content which contains the step description
                if "extracted_content=" in action:
                    content_part = action.split("extracted_content=")[1].split(",")[0]
                    # Clean up the content
                    content = content_part.strip("'\"")
                    
                    # Skip None values
                    if content == "None":
                        continue
                        
                    # Estimate timestamp for this step
                    step_timestamp = step_base_time + (i * step_interval)
                    
                    # Check if there's an error
                    if "error=" in action and not "error=None" in action:
                        error_part = action.split("error=")[1].split(",")[0]
                        error = error_part.strip("'\"")
                        if error != "None":
                            # Include the step number for error messages too
                            error_content = f"❌ Step {i+1}: {error}"
                            formatted += f"  {error_content}\n"
                            # Add to timeline
                            agent_steps_timeline.append({
                                "type": "agent_error",
                                "text": error_content,
                                "timestamp": step_timestamp
                            })
                            task_succeeded = False
                            continue
                    
                    # Check if this is a final message/conclusion step
                    is_final_message = "is_done=True" in action
                    
                    # Add emoji if not present, using a different emoji for the final message
                    if not content.startswith(("🔗", "🖱️", "⌨️", "🔍", "✅", "❌", "⚠️", "🏁")):
                        if is_final_message:
                            # Use a "finished" emoji rather than a checkmark for the completion message
                            content = f"🏁 {content}"
                        else:
                            content = f"✅ {content}"
                    
                    # If it has a checkmark but is a final message, replace with completion emoji
                    if content.startswith("✅") and is_final_message:
                        content = "🏁" + content[1:]
                    
                    # Format the output with step number for non-final messages
                    if not is_final_message:
                        # Add step number with 📍 emoji for display (i+1 to start from 1)
                        formatted_line = f"  📍 Step {i+1}: {content}"
                        # Store the content with step number for timeline
                        timeline_content = f"📍 Step {i+1}: {content}"
                    else:
                        # For final message, just use the content as is (already has 🏁)
                        formatted_line = f"  {content}"
                        timeline_content = content
                    
                    # Add to formatted output
                    formatted += formatted_line + "\n"
                    
                    # Add to timeline
                    agent_steps_timeline.append({
                        "type": "agent_step",
                        "text": timeline_content,
                        "timestamp": step_timestamp
                    })
        
        # Look for the 'done' action in the model outputs to extract the conclusion
        conclusion = ""
        if "'done':" in result_str or "\"done\":" in result_str:
            # Try to find the 'done' action and its text
            done_match = None
            if "'done':" in result_str:
                done_parts = result_str.split("'done':")[1].split("}")[0]
                done_match = done_parts
            elif "\"done\":" in result_str:
                done_parts = result_str.split("\"done\":")[1].split("}")[0]
                done_match = done_parts
                
            if done_match:
                # Extract the 'text' field from the done action
                if "'text':" in done_match:
                    text_part = done_match.split("'text':")[1].split(",")[0]
                    conclusion = text_part.strip("' \"")
                elif "\"text\":" in done_match:
                    text_part = done_match.split("\"text\":")[1].split(",")[0]
                    conclusion = text_part.strip("' \"")
                
                # Also check for success field in the done action
                if "'success': False" in done_match or "\"success\": False" in done_match:
                    task_succeeded = False
        
        # If we still don't have a conclusion, try the original method as fallback
        if not conclusion and "is_done=True" in result_str:
            for action in action_results:
                if "is_done=True" in action and "extracted_content=" in action:
                    content = action.split("extracted_content=")[1].split(",")[0].strip("'\"")
                    if content and content != "None":
                        conclusion = content
                        break
        
        # Add conclusion with appropriate status emoji
        if conclusion:
            # Use a neutral conclusion emoji instead of success/failure indicator
            formatted += f"\n📋 Conclusion:\n{conclusion}\n"
            
            # Add conclusion to timeline
            if agent_steps_timeline:
                # Set timestamp a bit after the last step
                conclusion_timestamp = agent_steps_timeline[-1]["timestamp"] + 2
            else:
                conclusion_timestamp = time.time()
                
            agent_steps_timeline.append({
                "type": "conclusion",
                "text": f"📋 Conclusion: {conclusion}",
                "timestamp": conclusion_timestamp
            })
        
        # First identify console errors for easier debugging
        console_errors = []
        if console_logs:
            for log in console_logs:
                if log.get('type') == 'error':
                    console_errors.append(log.get('text', 'Unknown error'))
        
        # Show console errors first (if any)
        if console_errors:
            formatted += f"\n🔴 Console Errors:"
            formatted += format_error_list(
                console_errors,
                lambda i, error: f"  {i+1}. {error}\n"
            )
        
        # Identify failed network requests for easier debugging
        failed_requests = []
        if network_requests:
            for req in network_requests:
                # Check if it's an XHR/fetch request and has a failure status code (4xx or 5xx)
                is_xhr = req.get('resourceType') == 'xhr' or req.get('resourceType') == 'fetch'
                status = req.get('response_status')
                if is_xhr and status and (status >= 400):
                    failed_requests.append({
                        'url': req.get('url', 'Unknown URL'),
                        'method': req.get('method', 'GET'),
                        'status': status
                    })
        
        # Show failed network requests next (if any)
        if failed_requests:
            formatted += f"\n❌ Failed Network Requests:"
            formatted += format_error_list(
                failed_requests,
                lambda i, req: f"  {i+1}. {req['method']} {req['url']} - Status: {req['status']}\n"
            )
        
        # Then show all console logs
        all_console_logs = []
        if console_logs:
            all_console_logs = list(console_logs)  # Convert deque to list for easier handling
        
        formatted += f"\n🖥️ All Console Logs:"
        formatted += format_error_list(
            all_console_logs,
            lambda i, log: f"  {i+1}. [{log.get('type', 'log')}] {log.get('text', 'Unknown message')}\n"
        )
        
        # Finally show all network requests
        all_network_requests = []
        if network_requests:
            all_network_requests = list(network_requests)  # Convert deque to list
        
        formatted += f"\n🌐 All Network Requests:"
        formatted += format_error_list(
            all_network_requests,
            lambda i, req: f"  {i+1}. {req.get('method', 'GET')} {req.get('url', 'Unknown URL')} - Status: {req.get('response_status', 'N/A')}\n"
        )
        
        # Add a chronological timeline of all events
        # Combine all events into a single list
        all_events = []
        
        # Add console logs to events
        for log in all_console_logs:
            all_events.append({
                "type": "console",
                "subtype": log.get('type', 'log'),
                "text": log.get('text', 'Unknown message'),
                "timestamp": log.get('timestamp', 0)
            })
        
        # Add network requests to events
        for req in all_network_requests:
            # Add request
            all_events.append({
                "type": "network_request",
                "method": req.get('method', 'GET'),
                "url": req.get('url', 'Unknown URL'),
                "timestamp": req.get('timestamp', 0)
            })
            
            # Add response if available
            if 'response_timestamp' in req:
                all_events.append({
                    "type": "network_response",
                    "method": req.get('method', 'GET'),
                    "url": req.get('url', 'Unknown URL'),
                    "status": req.get('response_status', 'N/A'),
                    "timestamp": req.get('response_timestamp', 0)
                })
        
        # Add agent steps to events
        all_events.extend(agent_steps_timeline)
        
        # Sort all events by timestamp
        all_events.sort(key=lambda x: x.get('timestamp', 0))
        
        # Format the timeline
        formatted += f"\n\n⏱️ Chronological Timeline of All Events:\n"
        
        timeline_text = ""
        for event in all_events:
            event_type = event.get('type')
            timestamp = event.get('timestamp', 0)
            
            # Format timestamp as HH:MM:SS.ms
            from datetime import datetime
            time_str = datetime.fromtimestamp(timestamp).strftime('%H:%M:%S.%f')[:-3]
            
            if event_type == 'console':
                subtype = event.get('subtype', 'log')
                text = event.get('text', '')
                emoji = "❌" if subtype == 'error' else "⚠️" if subtype == 'warning' else "🖥️"
                timeline_text += f"  {time_str} {emoji} Console [{subtype}]: {text}\n"
                
            elif event_type == 'network_request':
                method = event.get('method', 'GET')
                url = event.get('url', '')
                timeline_text += f"  {time_str} ➡️ Network Request: {method} {url}\n"
                
            elif event_type == 'network_response':
                method = event.get('method', 'GET')
                url = event.get('url', '')
                status = event.get('status', 'N/A')
                status_emoji = "❌" if str(status).startswith(('4', '5')) else "✅"
                timeline_text += f"  {time_str} ⬅️ Network Response: {method} {url} - Status: {status} {status_emoji}\n"
                
            elif event_type == 'agent_step':
                text = event.get('text', '')
                timeline_text += f"  {time_str} 🤖 {text}\n"
                
            elif event_type == 'agent_error':
                text = event.get('text', '')
                timeline_text += f"  {time_str} 🤖 Agent Error: {text}\n"
                
            elif event_type == 'conclusion':
                text = event.get('text', '')
                timeline_text += f"  {time_str} 🤖 {text}\n"
        
        # Truncate if necessary
        if len(timeline_text) > MAX_TIMELINE_CHARS:
            truncated_text = timeline_text[:MAX_TIMELINE_CHARS]
            # Try to end at a newline if possible
            last_newline = truncated_text.rfind('\n')
            if last_newline > MAX_TIMELINE_CHARS * 0.9:  # Only if we're not losing too much
                truncated_text = truncated_text[:last_newline+1]
                
            formatted += truncated_text
            formatted += f"  ... [Timeline truncated, {len(timeline_text) - len(truncated_text)} more characters not shown]\n"
        else:
            formatted += timeline_text
    
    except Exception as e:
        # If parsing fails, return a simpler message with the raw result
        return f"{formatted}⚠️ Result parsing failed: {e}\nRaw result: {result_str[:200]}...\n"
    
    return formatted
