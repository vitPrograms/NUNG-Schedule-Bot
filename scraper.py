import os
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import re
from collections import defaultdict
import urllib.parse

load_dotenv()

def get_schedule_html_by_post(group_name: str):
    """
    Fetches the schedule HTML page using a POST request with the group name.
    """
    try:
        url = "https://dekanat.nung.edu.ua/cgi-bin/timetable.cgi?n=700"
        # URL-encode the group name with windows-1251 encoding
        encoded_group_name = urllib.parse.quote(group_name, encoding='windows-1251')
        
        payload = f"faculty=0&teacher=&course=0&group={encoded_group_name}&sdate=&edate=&n=700"
        
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
        }

        response = requests.post(url, data=payload, headers=headers)
        response.raise_for_status()
        response.encoding = 'windows-1251'
        return response.text
    except requests.exceptions.RequestException as e:
        print(f"Error fetching schedule for group {group_name} via POST: {e}")
        return None

def get_schedule_html(group_id_or_name: str):
    """
    Fetches the schedule HTML page.
    If the input is a number (ID), uses GET.
    If the input is a string (name), uses POST.
    """
    if isinstance(group_id_or_name, str) and group_id_or_name.lstrip('-').isdigit():
        # It's a group ID
        try:
            url = f"https://dekanat.nung.edu.ua/cgi-bin/timetable.cgi?n=700&group={group_id_or_name}"
            response = requests.get(url)
            response.raise_for_status()
            response.encoding = 'windows-1251'
            return response.text
        except requests.exceptions.RequestException as e:
            print(f"Error fetching schedule for group ID {group_id_or_name}: {e}")
            return None
    else:
        # It's a group name, use POST
        return get_schedule_html_by_post(group_id_or_name)

def parse_lesson_details(details_html):
    """Parses the details of a single lesson."""
    lesson_info = defaultdict(list)
    
    # --- Corrected Link Extraction ---
    # First, extract all full links from <a> tags to avoid truncated URLs from text
    links = [a['href'] for a in details_html.find_all('a', href=True)]
    if links:
        lesson_info['links'] = sorted(list(set(links))) # Store unique links
    
    lines = [line.strip() for line in details_html.get_text('\n').split('\n') if line.strip()]

    # Regex patterns
    subject_pattern = re.compile(r'^\*\((.+?)\)\s*(.+)$')
    # A more specific pattern for groups like ІПм-24-1
    group_name_pattern = re.compile(r'[\w\s-]+-\d+-\d+') 
    teacher_pattern = re.compile(r'викладач\s(.+)', re.IGNORECASE)
    link_pattern = re.compile(r'https?://\S+') # This was the missing line
    subgroup_pattern = re.compile(r'підгр\.\s*\d', re.IGNORECASE)

    subject_found = False
    
    for line in lines:
        # Check for subject and type
        subject_match = subject_pattern.match(line)
        if subject_match:
            lesson_info['type'] = subject_match.group(1).strip()
            lesson_info['subject'] = subject_match.group(2).strip()
            subject_found = True
            continue

        # Check for teacher
        teacher_match = teacher_pattern.search(line)
        if teacher_match:
            lesson_info['teachers'].append(teacher_match.group(1).strip())
            continue

        # Check for groups
        group_matches = group_name_pattern.findall(line)
        if group_matches:
            lesson_info['groups'].extend([g.strip() for g in group_matches])
            
        # Check for subgroup
        subgroup_match = subgroup_pattern.search(line)
        if subgroup_match:
            lesson_info['subgroup'] = subgroup_match.group(0).strip()

        # We no longer need to parse links from text, as we get them from hrefs above
        
    if not subject_found:
        # Fallback: Find the most likely candidate for the subject name
        for line in lines:
            is_teacher = teacher_pattern.search(line)
            is_link = link_pattern.search(line)
            is_group_line = group_name_pattern.search(line)
            is_remote_word = 'дистанційно' in line.lower()
            
            if not is_teacher and not is_link and not is_group_line and not is_remote_word:
                # This line is likely the subject
                lesson_info['subject'] = line
                # Try to extract a lesson type like (Л) or (Пр) from it
                type_match = re.search(r'\((.+?)\)', line)
                if type_match:
                    lesson_info['type'] = type_match.group(1)
                    # Clean the subject from the type part
                    lesson_info['subject'] = line.replace(f"({type_match.group(1)})", "").strip()
                break # Found subject, stop searching
    
    # Remove duplicate groups
    if 'groups' in lesson_info:
        lesson_info['groups'] = sorted(list(set(lesson_info['groups'])))

    # Clean up empty lists
    for key in ['teachers', 'groups', 'links']:
        if not lesson_info.get(key):
            if key in lesson_info:
                del lesson_info[key]

    return dict(lesson_info)


def parse_unique_subjects(html_content: str) -> list[str]:
    """
    Parses the HTML content to find all unique subject names.
    """
    if not html_content:
        return []

    soup = BeautifulSoup(html_content, 'lxml')
    subjects = set()
    
    # The same logic as in parse_schedule to find lesson details
    day_divs = soup.find_all('div', class_='col-md-6')
    for day_div in day_divs:
        rows = day_div.find_all('tr')
        for row in rows:
            cols = row.find_all('td')
            if len(cols) == 3:
                details_cell = cols[2]
                if details_cell.get_text(strip=True):
                    # We can reuse the detailed parser
                    # A bit inefficient to re-parse, but ensures consistency
                    html_content_str = str(details_cell)
                    html_content_str = html_content_str.replace('<img', 'LESSON_SEPARATOR<img')
                    lesson_html_parts = html_content_str.split('LESSON_SEPARATOR')
                    for part in lesson_html_parts:
                        if part.strip():
                            lesson_soup = BeautifulSoup(part, 'lxml')
                            if lesson_soup.get_text(strip=True):
                                lesson_info = parse_lesson_details(lesson_soup)
                                if 'subject' in lesson_info:
                                    # Clean up the subject name from type like (Л), (Пр)
                                    subject_name = lesson_info['subject']
                                    subject_name = re.sub(r'\s*\((Л|Пр|Лаб)\)$', '', subject_name, flags=re.IGNORECASE).strip()
                                    subjects.add(subject_name)

    return sorted(list(subjects))


def parse_schedule(html_content):
    """
    Parses the HTML content of the schedule page and extracts the schedule.
    """
    if not html_content:
        return None

    soup = BeautifulSoup(html_content, 'lxml')
    schedule = {}

    # The schedule for each day is in a div with class 'col-md-6'
    day_divs = soup.find_all('div', class_='col-md-6')

    for day_div in day_divs:
        # Get the date
        date_header = day_div.find('h4')
        if not date_header:
            continue
        
        # Extract date and day of week more reliably
        date_str = date_header.contents[0].strip()
        day_of_week = date_header.find('small').get_text(strip=True) if date_header.find('small') else ''
        
        schedule[date_str] = {
            "day_of_week": day_of_week,
            "lessons": []
        }

        # Get the table with lessons for the day
        table = day_div.find('table', class_='table')
        if not table:
            continue

        rows = table.find_all('tr')
        for row in rows:
            cols = row.find_all('td')
            if len(cols) != 3:
                continue

            lesson_num = cols[0].get_text(strip=True)
            time_range = cols[1].get_text(separator='-', strip=True)
            details_cell = cols[2]

            if not details_cell.get_text(strip=True):
                continue
            
            # we need to find distinct lesson blocks.
            # A good separator seems to be a <br> tag followed by an empty line,
            # or simply the presence of another <img> tag indicating a new lesson.
            
            lessons_in_slot = []
            html_content_str = str(details_cell)
            
            # We split the inner HTML of the cell by occurrences that signal a new lesson
            # This is a heuristic: new lessons often start with an <img> tag.
            # We add a dummy separator before each img tag to split on.
            html_content_str = html_content_str.replace('<img', 'LESSON_SEPARATOR<img')
            
            lesson_html_parts = html_content_str.split('LESSON_SEPARATOR')

            for part in lesson_html_parts:
                if not part.strip():
                    continue
                # Each part is a string, so we parse it back to a BeautifulSoup object
                lesson_soup = BeautifulSoup(part, 'lxml')
                if lesson_soup.get_text(strip=True):
                    lessons_in_slot.append(parse_lesson_details(lesson_soup))


            schedule[date_str]["lessons"].append({
                "lesson_number": lesson_num,
                "time": time_range,
                "lessons_info": lessons_in_slot,
            })

    return schedule

if __name__ == '__main__':
    # For testing purposes
    html = get_schedule_html(-1985) # Example group ID
    if html:
        parsed_schedule = parse_schedule(html)
        if parsed_schedule:
            import json
            # Print the schedule in a readable format
            print(json.dumps(parsed_schedule, indent=2, ensure_ascii=False))
